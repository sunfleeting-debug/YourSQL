'use strict';
const $=id=>document.getElementById(id);
const state={token:'',tables:[],selected:null,results:[],resultIndex:0,view:'data',busy:false,resultsOpen:true,resultsHeight:360,history:[],tabs:[{name:'查询 1',sql:'-- 欢迎使用 YourSQL\n-- 从右上角选择测试用例，载入 SQL 后执行。\n'}],active:0,elapsed:0};
const testCases={
 crud:{name:'基础 CRUD',sql:`-- 基础 CRUD：建表、插入、查询、更新、删除
DROP TABLE IF EXISTS demo_crud;
CREATE TABLE demo_crud (
  id INT PRIMARY KEY,
  name VARCHAR(40),
  age INT
);
INSERT INTO demo_crud VALUES
  (1, 'Alice', 20),
  (2, 'Bob', 17),
  (3, 'Carol', 22);
SELECT * FROM demo_crud ORDER BY id;
UPDATE demo_crud SET age = age + 1 WHERE id = 2;
DELETE FROM demo_crud WHERE id = 3;
SELECT * FROM demo_crud ORDER BY id;`},
 types:{name:'数据类型与 NULL',sql:`-- 数据类型：INT、VARCHAR、BOOL、NULL
DROP TABLE IF EXISTS demo_types;
CREATE TABLE demo_types (
  id INT,
  name VARCHAR(40),
  active BOOL,
  note VARCHAR(40)
);
INSERT INTO demo_types VALUES
  (1, 'Alice', TRUE, NULL),
  (2, 'Bob', FALSE, 'inactive'),
  (3, 'Carol', TRUE, 'online');
SELECT id, name, active, note, note IS NULL AS note_missing
FROM demo_types ORDER BY id;`},
 query:{name:'条件查询与排序',sql:`-- 条件、算术表达式、DISTINCT、ORDER BY、LIMIT
DROP TABLE IF EXISTS demo_query;
CREATE TABLE demo_query (
  id INT,
  name VARCHAR(40),
  age INT,
  score INT
);
INSERT INTO demo_query VALUES
  (1, 'Alice', 20, 95),
  (2, 'Bob', 17, 88),
  (3, 'Carol', 20, 95),
  (4, 'Dan', 21, 71);
SELECT name, age + 1 AS next_age, score
FROM demo_query
WHERE age > 18 AND score >= 90
ORDER BY score DESC, name ASC
LIMIT 3;
SELECT DISTINCT age FROM demo_query ORDER BY age DESC LIMIT 3;`},
 plan:{name:'执行计划与优化',sql:`-- 查看 AST、优化前后执行计划和生效规则
DROP TABLE IF EXISTS demo_plan;
CREATE TABLE demo_plan (
  id INT,
  name VARCHAR(40),
  age INT,
  score INT
);
EXPLAIN SELECT name
FROM demo_plan
WHERE 1 = 1 AND age > 10 + 8 AND score >= 90;`},
 error_lexer:{name:'错误：词法',sql:`-- 词法错误：非法字符 @
SELECT @ FROM missing_table;`},
 error_syntax:{name:'错误：语法',sql:`-- 语法错误：WHERE 后缺少条件
SELECT id FROM missing_table WHERE;`},
 error_semantic:{name:'错误：语义',sql:`-- 语义错误：列不存在
DROP TABLE IF EXISTS demo_error_semantic;
CREATE TABLE demo_error_semantic (id INT, name VARCHAR(40));
SELECT missing FROM demo_error_semantic;`},
 error_type:{name:'错误：类型',sql:`-- 类型错误：INT 与 VARCHAR 不能相加
DROP TABLE IF EXISTS demo_error_type;
CREATE TABLE demo_error_type (id INT, name VARCHAR(40));
SELECT * FROM demo_error_type WHERE id + name > 1;`},
 lifecycle:{name:'表生命周期',sql:`-- Catalog 持久化、表数据落盘和 DROP TABLE
DROP TABLE IF EXISTS demo_lifecycle;
CREATE TABLE demo_lifecycle (id INT, description VARCHAR(60));
INSERT INTO demo_lifecycle VALUES
  (1, 'created and persisted'),
  (2, 'ready to be dropped');
SELECT * FROM demo_lifecycle;
DROP TABLE demo_lifecycle;`}
};
function el(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function empty(title,description,error=false){const box=el('div',undefined,'empty'+(error?' error':''));box.append(el('div',error?'!':'>_', 'empty-symbol'),el('h2',title),el('p',description));return box;}
function setStatus(text,error=false){$('result-status').textContent=text;$('result-status').className=error?'error-text':'success-text';}
async function api(path,body){const response=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json','X-YourSQL-Token':state.token},body:JSON.stringify(body)}:{});const data=await response.json();if(!response.ok)throw Error(data.error||'请求失败');return data;}
function updateState(data){state.tables=data.tables;$('db-name').textContent=data.database;$('connection').textContent='已连接 · '+data.database;$('connection-dot').className='connected';$('table-count').textContent=data.tables.length;$('buffer-status').textContent='缓存命中率 '+Math.round((data.stats.hit_rate||0)*100)+'%  ·  磁盘读取 '+(data.stats.disk_reads||0)+'  ·  写入 '+(data.stats.disk_writes||0);if(!state.tables.some(t=>t.name===state.selected))state.selected=null;renderTree();}
async function refresh(){try{const data=await api('/api/state');state.token=data.token;updateState(data);$('run').disabled=state.busy;renderResults();}catch(error){$('connection').textContent='连接失败';$('connection-dot').className='';$('run').disabled=true;setStatus(error.message,true);}}
function renderTree(){const tree=$('tree');tree.replaceChildren();const tables=state.tables.filter(t=>t.name.toLowerCase().includes($('search').value.toLowerCase()));if(!tables.length){tree.append(el('p',state.tables.length?'没有匹配的数据表':'数据库还是空的，先创建一张表。','muted-note'));return;}for(const table of tables){const button=el('button',undefined,'table-item'+(state.selected===table.name?' active':''));button.append(el('span','▦'),el('span',table.name),el('small',String(table.columns.length)));button.title='查看 '+table.name+' 的结构';button.addEventListener('click',()=>{state.selected=table.name;renderTree();setView('structure');});button.addEventListener('dblclick',()=>newQuery('SELECT * FROM '+table.name+' LIMIT 100;',table.name));tree.append(button);if(state.selected===table.name){const cols=el('div',undefined,'column-list');table.columns.forEach(c=>{const row=el('div',undefined,'column');row.append(el('span',(c.primary_key?'◆ ':'')+c.name),el('span',c.type));cols.append(row);});tree.append(cols);}}}
function renderTabs(){const list=$('query-tabs');list.replaceChildren();state.tabs.forEach((tab,index)=>{const button=el('button',tab.name,'query-tab');button.setAttribute('role','tab');button.setAttribute('aria-selected',String(index===state.active));button.addEventListener('click',()=>{state.tabs[state.active].sql=$('editor').value;state.active=index;$('editor').value=tab.sql;renderTabs();updateEditor();});list.append(button);});}
function newQuery(sql='',name){state.tabs[state.active].sql=$('editor').value;state.tabs.push({name:name||'查询 '+(state.tabs.length+1),sql});state.active=state.tabs.length-1;$('editor').value=sql;renderTabs();updateEditor();$('editor').focus();}
function updateEditor(){const editor=$('editor');state.tabs[state.active].sql=editor.value;$('line-numbers').textContent=Array.from({length:editor.value.split('\n').length},(_,i)=>i+1).join('\n');const before=editor.value.slice(0,editor.selectionStart).split('\n');$('cursor').textContent='行 '+before.length+'，列 '+(before.at(-1).length+1);$('line-numbers').scrollTop=editor.scrollTop;highlightSQL();}
function setResultsHeight(height){const min=180;const max=Math.min(720,Math.max(min,window.innerHeight-180));state.resultsHeight=Math.max(min,Math.min(Math.round(height),max));const panel=$('results-panel');panel.style.height=state.resultsHeight+'px';const handle=$('results-resize-handle');handle.setAttribute('aria-valuemax',String(max));handle.setAttribute('aria-valuenow',String(state.resultsHeight));}
function setResultsDrawer(open){state.resultsOpen=open;$('results-panel').classList.toggle('collapsed',!open);const toggle=$('toggle-results');toggle.setAttribute('aria-expanded',String(open));toggle.title=open?'折叠查询结果':'展开查询结果';toggle.textContent=open?'⌄ 折叠':'⌃ 展开';if(open)setResultsHeight(state.resultsHeight);}
function resizeResults(event){if(!state.resultsOpen)return;event.preventDefault();const startY=event.clientY;const startHeight=state.resultsHeight;document.body.classList.add('resizing-results');const move=moveEvent=>setResultsHeight(startHeight+startY-moveEvent.clientY);const stop=()=>{document.removeEventListener('pointermove',move);document.body.classList.remove('resizing-results');};document.addEventListener('pointermove',move);document.addEventListener('pointerup',stop,{once:true});}
function nudgeResults(delta){setResultsHeight(state.resultsHeight+delta);}
function setView(view){state.view=view;setResultsDrawer(true);document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.view===view)));renderResults();}
function makeTable(columns,rows){const table=el('table');const head=el('thead');const tr=el('tr');tr.append(el('th','#','row-number'));columns.forEach(c=>tr.append(el('th',String(c))));head.append(tr);table.append(head);const body=el('tbody');rows.forEach((row,i)=>{const tr=el('tr');tr.append(el('td',String(i+1),'row-number'));row.forEach(value=>{const cell=el('td',value===null?'NULL':String(value),value===null?'null':typeof value==='number'?'numeric':undefined);cell.title=cell.textContent;tr.append(cell);});body.append(tr);});table.append(body);return table;}
function renderResults(){const content=$('result-content');content.replaceChildren();const result=state.results[state.resultIndex];$('export').disabled=state.view!=='data'||!result?.ok||!result?.columns.length;$('result-filter').disabled=state.view!=='data'||!result?.ok||!result?.columns.length;$('statement-label').hidden=!['data','plan'].includes(state.view)||state.results.length<2;
 if(state.view==='structure'){const table=state.tables.find(t=>t.name===state.selected);if(!table){content.append(empty('选择一张数据表','点击左侧表名查看字段、类型和约束。'));return;}const actions=el('div',undefined,'result-meta');actions.append(el('span',table.name+' · '+table.columns.length+' 个字段'));const browse=el('button','查询前 100 行 →','text-button');browse.addEventListener('click',()=>{newQuery('SELECT * FROM '+table.name+' LIMIT 100;',table.name);execute();});actions.append(browse);content.append(actions,makeTable(['字段名','类型','允许 NULL','主键'],table.columns.map(c=>[c.name,c.type,c.nullable?'是':'否',c.primary_key?'是':'否'])));return;}
 if(state.view==='history'){if(!state.history.length){content.append(empty('还没有查询记录','执行过的 SQL 会显示在这里，记录仅保留在当前页面。'));return;}for(const item of [...state.history].reverse()){const row=el('div',undefined,'history-item');row.append(el('small',item.time),el('code',item.sql));const button=el('button','载入','secondary');button.addEventListener('click',()=>newQuery(item.sql));row.append(button);content.append(row);}return;}
 if(!result){content.append(empty('让数据回答你的问题','输入 SQL 后点击执行，或选择左侧数据表查看结构。'));return;}
 if(!result.ok){content.append(empty(result.stage+' 阶段错误',result.message,true));return;}
 if(state.view==='plan'){if(!result.plan_before){content.append(empty('这条语句没有执行计划','尝试执行 SELECT 或 EXPLAIN 语句。'));return;}const grid=el('div',undefined,'plan-grid');[['优化前',result.plan_before],['优化后',result.plan_after]].forEach(([label,plan])=>{const section=el('section');section.append(el('h3',label),el('pre',plan));grid.append(section);});content.append(grid,el('div',result.rules.length?'生效规则：'+result.rules.join(' / '):'本次没有触发优化规则。','rules'));return;}
 if(result.columns.length){const term=$('result-filter').value.toLocaleLowerCase();const rows=result.rows.filter(row=>row.some(value=>String(value===null?'NULL':value).toLocaleLowerCase().includes(term)));$('filter-count').textContent=term?'匹配 '+rows.length+' / '+result.rows.length+' 行':'仅筛选已加载的行';content.append(makeTable(result.columns,rows));if(term&&!rows.length&&result.rows.length)content.append(empty('没有匹配的记录','换一个关键词，或清空筛选条件。'));if(!result.rows.length)content.append(empty('查询完成，没有匹配的记录','尝试调整 WHERE 条件，或向表中插入数据。'));}else content.append(empty('语句执行成功',result.message));}
function selectResult(index){state.resultIndex=index;const result=state.results[index];if(result){setStatus(result.ok?(result.columns.length?'查询成功 · 返回 '+result.row_count+' 行':result.message):result.stage+' · '+result.message,!result.ok);$('result-count').textContent=result.columns.length?(result.truncated?'显示前 1,000 行，共 '+result.row_count+' 行':result.row_count+' 行结果'):'语句 '+(index+1)+' / '+state.results.length;}renderResults();}
async function execute(){if(state.busy||!state.token)return;const editor=$('editor');const sql=(editor.value.slice(editor.selectionStart,editor.selectionEnd)||editor.value).trim();if(!sql){setStatus('请先输入 SQL 语句。',true);editor.focus();return;}state.busy=true;$('run').disabled=true;$('run').textContent='执行中…';setStatus('正在执行查询…');try{const data=await api('/api/execute',{sql});state.results=data.results;$('result-filter').value='';state.elapsed=data.elapsed_ms;updateState(data);state.history.push({sql,time:new Date().toLocaleTimeString()});if(state.history.length>50)state.history.shift();$('statement').replaceChildren();data.results.forEach((r,i)=>{const option=el('option',(i+1)+' · '+(r.ok?'成功':'错误'));option.value=i;$('statement').append(option);});const firstError=data.results.findIndex(r=>!r.ok);state.resultIndex=firstError>=0?firstError:Math.max(0,data.results.length-1);$('statement').value=state.resultIndex;setView('data');selectResult(state.resultIndex);if(!data.results.length){setStatus('没有可执行的语句。');$('result-count').textContent='0 条语句';}else if(firstError>=0)setStatus('部分或全部语句失败；其他语句可能已生效。'+data.results[firstError].message,true);$('elapsed').textContent='执行耗时 '+data.elapsed_ms+' ms';}catch(error){state.results=[];setStatus(error.message,true);$('result-content').replaceChildren(empty('执行失败',error.message,true));$('export').disabled=true;$('result-count').textContent='未获得执行结果';$('elapsed').textContent='—';$('statement-label').hidden=true;}finally{state.busy=false;$('run').disabled=!state.token;$('run').textContent='▶ 执行 SQL';}}
function download(text,name,type){const url=URL.createObjectURL(new Blob([text],{type}));const link=el('a');link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('export').addEventListener('click',()=>{const r=state.results[state.resultIndex];if(!r?.columns.length)return;const cell=value=>{let text=value===null?'':String(value);if(typeof value==='string'&&/^[=+@\-\t\r]/.test(text))text="'"+text;return '"'+text.replaceAll('"','""')+'"';};download('\uFEFF'+[r.columns,...r.rows].map(row=>row.map(cell).join(',')).join('\r\n'),'query-result.csv','text/csv;charset=utf-8');});
 $('run').addEventListener('click',execute);$('refresh').addEventListener('click',refresh);$('search').addEventListener('input',renderTree);$('new-query').addEventListener('click',()=>newQuery());$('test-case').addEventListener('change',event=>{const testCase=testCases[event.target.value];if(testCase)newQuery(testCase.sql,testCase.name);event.target.value='';});$('toggle-results').addEventListener('click',()=>setResultsDrawer(!state.resultsOpen));$('results-resize-handle').addEventListener('pointerdown',resizeResults);$('results-resize-handle').addEventListener('keydown',event=>{if(event.key==='ArrowUp'){event.preventDefault();nudgeResults(24);}if(event.key==='ArrowDown'){event.preventDefault();nudgeResults(-24);}if(event.key==='Home'){event.preventDefault();setResultsHeight(180);}if(event.key==='End'){event.preventDefault();setResultsHeight(720);}});$('statement').addEventListener('change',e=>selectResult(Number(e.target.value)));document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
$('editor').addEventListener('input',updateEditor);$('editor').addEventListener('click',updateEditor);$('editor').addEventListener('keyup',updateEditor);$('editor').addEventListener('scroll',()=>{$('line-numbers').scrollTop=$('editor').scrollTop;syncHighlight();});$('editor').addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();execute();}if(event.key==='Tab'){event.preventDefault();const e=event.target;e.setRangeText('  ',e.selectionStart,e.selectionEnd,'end');updateEditor();}});
$('open-file').addEventListener('click',()=>$('file-input').click());$('file-input').addEventListener('change',async event=>{const file=event.target.files[0];if(file){try{if(file.size>1000000)throw Error('SQL 文件不能超过 1 MB。');newQuery(await file.text(),file.name);}catch(error){setStatus(error.message,true);}event.target.value='';}});$('save-file').addEventListener('click',()=>download($('editor').value,'query.sql','text/plain;charset=utf-8'));
$('result-filter').addEventListener('input',renderResults);
 $('editor').value=state.tabs[0].sql;renderTabs();updateEditor();setResultsDrawer(true);renderResults();refresh();

// Render text nodes only: database text and SQL are never interpreted as HTML.
function highlightSQL(){
 const source=$('editor').value;
 const pattern=/(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$))|('(?:''|[^'])*(?:'|$))|\b(SELECT|FROM|WHERE|ORDER|BY|ASC|DESC|LIMIT|INSERT|INTO|VALUES|CREATE|TABLE|DROP|UPDATE|SET|DELETE|EXPLAIN|DISTINCT|AS|AND|OR|NOT|NULL|IS|PRIMARY|KEY|INT|VARCHAR|BOOL|TRUE|FALSE|IF|EXISTS)\b|\b(\d+)\b/gi;
 const fragment=document.createDocumentFragment();let end=0;
 for(const match of source.matchAll(pattern)){
  fragment.append(document.createTextNode(source.slice(end,match.index)));
  fragment.append(el('span',match[0],match[1]?'sql-comment':match[2]?'sql-string':match[3]?'sql-keyword':'sql-number'));
  end=match.index+match[0].length;
 }
 fragment.append(document.createTextNode(source.slice(end)+'\n'));
 $('highlight').replaceChildren(fragment);syncHighlight();
}
function syncHighlight(){
 $('highlight').scrollTop=$('editor').scrollTop;
 $('highlight').scrollLeft=$('editor').scrollLeft;
}
