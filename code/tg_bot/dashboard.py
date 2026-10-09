"""Web dashboard for the TG bot: live transfer progress + history.
VERSION = "1.6.4"  # 2026-10-09: 安全加固(hmac+IP限流+输入校验)  # 2026-10-06: 正在传输标题显示排队剩余数  # 2026-10-05: 91porn 拆分独立面板  # 2026-10-04: run_91porn 走 systemd

Token auth via ?token=. Read-only except explicit actions:
- POST /api/cancel {key}: cancel a running task
- POST /api/del_history {id}: delete a history record
"""
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_PDL_CATS = frozenset((
    'now_month_comment', 'now_hot', 'hd', 'play', 'new_update', 'original',
    'now_month_hot', 'ten_minutes', 'twenty_minutes', 'now_month_collect',
    'month_hot', 'max_collect'))
_FAILS = {}  # ip -> [失败时间戳]
_FAIL_MAX, _FAIL_WINDOW = 10, 300


def _blocked(ip):
    now = time.time()
    lst = [t for t in _FAILS.get(ip, []) if now - t < _FAIL_WINDOW]
    _FAILS[ip] = lst
    return len(lst) >= _FAIL_MAX


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jack传送站 · 面板</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  /* Element Plus 配色 */
  --primary:#409eff; --primary-light:#79bbff; --primary-dark:#337ecc;
  --success:#67c23a; --warning:#e6a23c; --danger:#f56c6c; --info:#909399;
  --bg:#f2f3f5; --card:#ffffff; --border:#dcdfe6; --border-light:#e4e7ed;
  --text:#303133; --text-regular:#606266; --text-secondary:#909399; --text-placeholder:#a8abb2;
  --radius:6px; --shadow:0 2px 8px rgba(0,0,0,.08);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#141414; --card:#1d1e1f; --border:#363637; --border-light:#2a2b2c;
    --text:#e5eaf3; --text-regular:#cfd3dc; --text-secondary:#a3a6ad; --text-placeholder:#6c6e72;
    --shadow:0 2px 8px rgba(0,0,0,.3);
  }
}
body{background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  padding:16px;max-width:720px;margin:0 auto;font-size:14px;line-height:1.6;
  -webkit-font-smoothing:antialiased}
/* 顶栏 */
.header{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);
  border:none;border-radius:var(--radius);
  padding:20px 24px;margin-bottom:16px;box-shadow:0 4px 20px rgba(0,0,0,.25);
  display:flex;align-items:center;gap:12px;color:#fff;position:relative;overflow:hidden}
.header::before{content:'';position:absolute;top:0;left:0;right:0;bottom:0;
  background:radial-gradient(circle at 80% 20%,rgba(255,255,255,.08) 0%,transparent 50%)}
.header h1{font-size:22px;font-weight:700;letter-spacing:1px;position:relative;z-index:1;
  background:linear-gradient(90deg,#fff,#a8d8ff);-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;background-clip:text}
.header .subtitle{font-size:11px;color:#a8d8ff;margin-left:auto;position:relative;z-index:1;
  background:rgba(255,255,255,.12);padding:4px 12px;border-radius:20px;letter-spacing:2px}
/* 分区标题 */
.section-title{font-size:14px;font-weight:600;color:var(--text);
  margin:20px 0 12px;padding-left:10px;border-left:3px solid var(--primary)}
.section-title .actions{float:right;font-weight:400}
/* 卡片 */
.card{background:var(--card);border:1px solid var(--border-light);border-radius:var(--radius);
  padding:16px 20px;margin-bottom:12px;box-shadow:var(--shadow)}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px}
.row:last-child{margin-bottom:0}
.name{font-size:14px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.phase{font-size:12px;color:var(--success);white-space:nowrap}
/* 进度条 - Element 风格 */
.bar{height:6px;background:var(--border-light);border-radius:3px;overflow:hidden;margin:10px 0}
.bar>i{display:block;height:100%;background:var(--primary);border-radius:3px;transition:width .5s ease}
.meta{font-size:12px;color:var(--text-secondary)}
.mut{font-size:12px;color:var(--text-secondary);margin-top:6px}
/* 历史记录 */
.hist{font-size:13px;padding:12px 0;border-bottom:1px solid var(--border-light);
  display:flex;justify-content:space-between;align-items:center;gap:12px}
.hist:last-child{border-bottom:none}
.hist .info{flex:1;min-width:0}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:4px;font-weight:500}
.tag-ok{background:#f0f9eb;color:var(--success);border:1px solid #e1f3d8}
.tag-fail{background:#fef0f0;color:var(--danger);border:1px solid #fde2e2}
.tag-cancel{background:#fdf6ec;color:var(--warning);border:1px solid #faecd8}
@media (prefers-color-scheme:dark){
  .tag-ok{background:rgba(103,194,58,.12);border-color:rgba(103,194,58,.3)}
  .tag-fail{background:rgba(245,108,108,.12);border-color:rgba(245,108,108,.3)}
  .tag-cancel{background:rgba(230,162,60,.12);border-color:rgba(230,162,60,.3)}
}
.empty{color:var(--text-placeholder);font-size:13px;padding:20px 0;text-align:center}
/* 按钮 - Element 风格 */
.btn{border:1px solid var(--border);background:var(--card);color:var(--text-regular);
  border-radius:var(--radius);padding:8px 16px;font-size:13px;font-weight:400;cursor:pointer;
  white-space:nowrap;transition:all .15s;line-height:1}
.btn:hover{color:var(--primary);border-color:var(--primary-light);background:#ecf5ff}
@media (prefers-color-scheme:dark){.btn:hover{background:rgba(64,158,255,.1)}}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{background:var(--primary);border-color:var(--primary);color:#fff}
.btn-primary:hover{background:var(--primary-light);border-color:var(--primary-light);color:#fff}
.btn-danger{background:var(--danger);border-color:var(--danger);color:#fff}
.btn-danger:hover{background:#f78989;border-color:#f78989;color:#fff}
.btn-text{background:transparent;border:none;color:var(--primary);padding:4px 8px}
.btn-text:hover{background:#ecf5ff}
@media (prefers-color-scheme:dark){.btn-text:hover{background:rgba(64,158,255,.1)}}
.btn-sm{padding:6px 12px;font-size:12px}
/* 输入框 - Element 风格 */
input[type="number"],input[type="text"]{background:var(--card);color:var(--text);
  border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;font-size:13px;width:4em;
  transition:border-color .15s}
input[type="number"]:focus,input[type="text"]:focus{outline:none;border-color:var(--primary)}
input[type="checkbox"]{accent-color:var(--primary);width:15px;height:15px;vertical-align:middle}
label{font-size:13px;color:var(--text-regular);display:inline-flex;align-items:center;gap:6px}
.dot{color:var(--success);font-size:10px;margin-right:4px}
#updated{font-size:12px;color:var(--text-placeholder);margin-top:24px;text-align:center}
.schedule-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;color:var(--text-regular)}
.stat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px}
.stat-card{background:var(--card);border:1px solid var(--border-light);border-radius:var(--radius);
  padding:16px;text-align:center;box-shadow:var(--shadow)}
.stat-card .num{font-size:24px;font-weight:600;color:var(--primary)}
.stat-card .label{font-size:12px;color:var(--text-secondary);margin-top:4px}
</style></head><body>
<div class="header">
  <span class="subtitle">管理面板</span>
</div>
<div class="section-title">正在传输 <span id="cnt"></span></div>
<div id="active"></div>
<div class="section-title">91porn 每日抓取
  <span class="actions">
    <button class="btn btn-sm btn-primary" data-act="run91">立即运行</button>
    <button class="btn btn-sm btn-danger" data-act="stop91">取消任务</button>
  </span>
</div>
<div class="card">
  <div class="schedule-row">每天
    <input id="p91hour" type="text" inputmode="numeric" style="width:60px;text-align:center"> :
    <input id="p91min" type="text" inputmode="numeric" style="width:60px;text-align:center"> 运行
    <label><input type="checkbox" id="p91en"> 启用</label>
    <button class="btn btn-sm" data-act="save91">保存</button>
  </div>
  <div class="schedule-row">分类：
    <select id="p91cat">
      <option value="">首页最新（默认）</option>
      <option value="https://91porn.com/v.php?category=rf">本月最热</option>
      <option value="https://91porn.com/v.php?category=tf">收藏最多</option>
      <option value="https://91porn.com/v.php?category=mf">最多评论</option>
      <option value="custom">自定义URL…</option>
    </select>
    <input id="p91cat_url" type="text" placeholder="粘贴91porn列表页URL" style="width:280px;display:none">
  </div>
  <div class="mut" id="p91next"></div>
  <div id="p91"><div class="empty">加载中…</div></div>
</div>
<div class="section-title">91porna 自动抓取
  <span class="actions">
    <button class="btn btn-sm btn-primary" data-act="runPdlAuto">立即运行</button>
    <button class="btn btn-sm btn-danger" data-act="stopPdlAuto">取消任务</button>
  </span>
</div>
<div class="card">
  <div class="schedule-row">每天
    <input id="pdlauto_hour" type="text" inputmode="numeric" value="3" style="width:60px;text-align:center"> :
    <input id="pdlauto_min" type="text" inputmode="numeric" value="0" style="width:60px;text-align:center"> 运行
    <label><input type="checkbox" id="pdlauto_en" checked> 启用</label>
    <button class="btn btn-sm" data-act="savePdlAuto">保存</button>
  </div>
  <div class="schedule-row">分类：
    <select id="pdlauto_cat">
      <option value="now_month_comment">本月讨论</option>
      <option value="now_hot">当前最热</option>
      <option value="hd">高清</option>
      <option value="play">正在播放</option>
      <option value="new_update">最近更新</option>
      <option value="original">91原创</option>
      <option value="now_month_hot">本月最热</option>
      <option value="ten_minutes">10分钟以上</option>
      <option value="twenty_minutes">20分钟以上</option>
      <option value="now_month_collect">本月收藏</option>
      <option value="month_hot">每月最热</option>
      <option value="max_collect">收藏最多</option>
    </select>
    页数 <input id="pdlauto_pages" type="text" inputmode="numeric" value="2" style="width:60px;text-align:center">
    最多 <input id="pdlauto_limit" type="text" inputmode="numeric" value="20" style="width:60px;text-align:center"> 条
  </div>
  <div class="mut" id="pdlauto_next"></div>
  <div id="pdlauto_status">状态加载中…</div>
  <div id="pdlauto_counts"></div>
  <div class="schedule-row" style="margin-top:8px">
    <button class="btn btn-sm" data-act="togglePdlLog">查看日志</button>
  </div>
  <pre id="pdlauto_log" style="display:none;max-height:200px;overflow-y:auto;background:#1a1a2e;color:#a8d8ff;font-size:11px;padding:10px;border-radius:8px;white-space:pre-wrap"></pre>
</div>
<div class="section-title">同步记录
  <span class="actions"><button class="btn btn-sm btn-text" data-act="clearHist">清空</button></span>
</div>
<div id="hist" class="card"></div>
<div id="updated"></div>
<script>
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function _tok(){
  // 学 t3code：token 放 location.hash，不经过 query，不进服务器日志
  var m = location.hash.match(/token=([^&]+)/);
  if(m) return decodeURIComponent(m[1]);
  var q = location.search.match(/[?&]token=([^&]+)/);
  return q ? decodeURIComponent(q[1]) : '';
}
function api(path,data,btn){
  if(btn){btn.disabled=true;btn.dataset.orig=btn.textContent;btn.textContent='处理中…';}
  return fetch(path,{
    method:'POST',
    headers:{'X-Token':_tok(),'Content-Type':'application/json'},
    body:JSON.stringify(data||{})
  }).then(function(r){
    if(!r.ok) throw new Error('HTTP '+r.status+'，请稍后重试');
    return r.json();
  }).catch(function(e){
    alert('操作失败：'+e.message);
    throw e;
  }).finally(function(){
    if(btn){btn.disabled=false;btn.textContent=btn.dataset.orig||btn.textContent;}
  });
}
function cancelTask(key,btn){
  if(!confirm('确定取消这个传输任务吗？')) return;
  api('api/cancel',{key:key},btn).then(load);
}
function delHist(id,btn){
  if(!confirm('删除这条同步记录？')) return;
  api('api/del_history',{id:id},btn).then(load);
}
function clearHist(btn){
  if(!confirm('确定清空所有同步记录吗？')) return;
  api('api/clear_history',{},btn).then(load);
}
// 事件委托：避免 onclick 里嵌套引号转义
function run91(btn){
  if(!confirm('现在运行 91porn 抓取+上传？')) return;
  api('api/run_91porn',{},btn).then(function(){ load91(); });
}
function stop91(btn){
  if(!confirm('取消正在运行的 91porn 任务？')) return;
  api('api/stop_91porn',{},btn).then(function(){ load91(); });
}
function togglePdlLog(btn){
  var el = document.getElementById('pdlauto_log');
  if(el.style.display === 'none'){
    el.style.display = 'block';
    btn.textContent = '隐藏日志';
    loadPdlLog();
    // 每5秒自动刷新日志
    if(!window._pdlLogTimer) window._pdlLogTimer = setInterval(loadPdlLog, 5000);
  } else {
    el.style.display = 'none';
    btn.textContent = '查看日志';
    if(window._pdlLogTimer){ clearInterval(window._pdlLogTimer); window._pdlLogTimer = null; }
  }
}
function loadPdlLog(){
  fetch('api/pdl_auto_log',{headers:{'X-Token':_tok()}}).then(function(r){
    return r.ok ? r.json() : null;
  }).then(function(d){
    if(d && d.ok){
      var el = document.getElementById('pdlauto_log');
      el.textContent = d.log || '暂无日志';
      el.scrollTop = el.scrollHeight;
    }
  }).catch(function(){});
}
function stopPdlAuto(btn){
  if(!confirm('取消正在运行的 91porna 抓取任务？')) return;
  api('api/pdl_auto_stop',{},btn).then(function(){
    loadPdlAuto();
  });
}
function savePdlAuto(btn){
  var hour = parseInt(document.getElementById('pdlauto_hour').value, 10);
  var minute = parseInt(document.getElementById('pdlauto_min').value, 10);
  var enabled = document.getElementById('pdlauto_en').checked;
  var cat = document.getElementById('pdlauto_cat').value;
  var pages = parseInt(document.getElementById('pdlauto_pages').value, 10) || 2;
  var limit = parseInt(document.getElementById('pdlauto_limit').value, 10) || 20;
  api('api/pdl_auto',{hour:hour,minute:minute,enabled:enabled,category:cat,pages:pages,limit:limit},btn).then(function(d){
    document.getElementById('pdlauto_next').textContent = d.ok ? ('已保存: '+(d.next||'')) : ('失败: '+(d.error||''));
    loadPdlAuto();
  });
}
function runPdlAuto(btn){
  if(!confirm('立即运行 91porna 自动抓取？')) return;
  document.getElementById('pdlauto_status').textContent = '启动中…';
  api('api/pdl_auto_run',{},btn).then(function(d){
    document.getElementById('pdlauto_status').textContent = d.ok ? '已启动，后台运行中' : ('失败: '+(d.error||''));
    setTimeout(loadPdlAuto, 2000);
  });
}
var _pdlFirstLoad = true;
function loadPdlAuto(){
  fetch('api/pdl_auto_status',{headers:{'X-Token':_tok()}}).then(function(r){
    return r.ok ? r.json() : null;
  }).then(function(d){
    if(!d || !d.ok) return;
    var s = d.result || {};
    // 只在第一次加载时写表单，之后不覆盖用户正在改的输入
    if(_pdlFirstLoad){
      if(s.hour != null) document.getElementById('pdlauto_hour').value = s.hour;
      if(s.minute != null) document.getElementById('pdlauto_min').value = s.minute;
      document.getElementById('pdlauto_en').checked = !!s.enabled;
      if(s.category) document.getElementById('pdlauto_cat').value = s.category;
      if(s.pages) document.getElementById('pdlauto_pages').value = s.pages;
      if(s.limit != null) document.getElementById('pdlauto_limit').value = s.limit;
      _pdlFirstLoad = false;
    }
    // 跟 91porn 每日抓取完全一致：定时器状态放 pdlauto_next，状态区只放 ●状态 / 统计 / 时间
    var nextEl = document.getElementById('pdlauto_next');
    if(s.enabled){
      var t = '每天 ' + String(s.hour).padStart(2,'0') + ':' + String(s.minute).padStart(2,'0') + ' 运行';
      if(s.next_run) t += '（下次 ' + esc(s.next_run) + '）';
      nextEl.textContent = t;
    } else {
      nextEl.textContent = '已停用';
    }
    var h = '';
    var phase = s.running ? '抓取中' : '完成';
    if(s.running && s.progress) phase += ' ' + s.progress;
    h += '<div><span class="dot">●</span> ' + esc(phase) + '</div>';
    var stats = [];
    if(s.scraped != null) stats.push('抓取 ' + s.scraped);
    if(s.downloaded != null) stats.push('下载 ' + s.downloaded);
    if(s.uploaded != null) stats.push('上传 ' + s.uploaded);
    if(stats.length) h += '<div class="mut">' + stats.join(' · ') + '</div>';
    if(s.last_run) h += '<div class="mut">上次：' + esc(s.last_run) + '</div>';
    document.getElementById('pdlauto_status').innerHTML = h;
    var cntEl = document.getElementById('pdlauto_counts');
    if(cntEl) cntEl.textContent = '';
  }).catch(function(){});
}
function save91(btn){
  var hour = parseInt(document.getElementById('p91hour').value, 10);
  var minute = parseInt(document.getElementById('p91min').value, 10);
  var enabled = document.getElementById('p91en').checked;
  var catSel = document.getElementById('p91cat');
  var cat = catSel.value;
  if(cat === 'custom'){
    cat = document.getElementById('p91cat_url').value.trim();
  }
  api('api/91porn_schedule',{hour:hour,minute:minute,enabled:enabled,category:cat},btn).then(function(){ load91(); });
}
function load91(){
  fetch('api/91porn_status',{headers:{'X-Token':_tok()}}).then(function(r){
    if(!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }).catch(function(){
    document.getElementById('p91').innerHTML = '<div class="empty">状态加载失败，请刷新重试</div>';
    return null;
  }).then(function(d){
    if(!d) return;
    var h = '';
    if(d.ok && d.result){
      var s = d.result;
      var phase = s.phase || '-';
      var m = phase.match(/(\d+)\/(\d+)/);
      var remain = '';
      if(m){
        var left = parseInt(m[2],10) - parseInt(m[1],10);
        if(left > 0) remain = '（剩 ' + left + ' 个）';
      }
      h = '<div><span class="dot">●</span> ' + esc(phase) + remain + '</div>';
      if(s.current){
        var cur = s.current;
        if(cur.length > 30) cur = cur.slice(0,27) + '...';
        h += '<div class="mut">当前：' + esc(cur);
        if(s.current_mb) h += '（' + s.current_mb + 'MB）';
        h += '</div>';
      }
      var stats = [];
      if(s.downloaded != null) stats.push('下载 ' + s.downloaded);
      if(s.uploaded != null) stats.push('上传 ' + s.uploaded);
      if(s.errors && s.errors.length) stats.push('失败 ' + s.errors.length);
      if(stats.length) h += '<div class="mut">' + stats.join(' · ') + '</div>';
      if(s.started) h += '<div class="mut">开始：' + esc(s.started) + '</div>';
    } else {
      h = '<div class="empty">尚未运行过</div>';
    }
    document.getElementById('p91').innerHTML = h;
  });
  fetch('api/91porn_schedule',{headers:{'X-Token':_tok()}}).then(function(r){ return r.json(); }).then(function(d){
    if(d.ok && d.result){
      var s = d.result;
      document.getElementById('p91hour').value = s.hour;
      document.getElementById('p91min').value = s.minute;
      document.getElementById('p91en').checked = !!s.enabled;
      document.getElementById('p91next').textContent = s.enabled ? ('下次运行：' + (s.next_run || '-')) : '已停用';
    }
  });
}
document.addEventListener('click', function(e){
  var b = e.target.closest('button[data-act]');
  if(!b||b.disabled) return;
  if(b.dataset.act=='cancel') cancelTask(b.dataset.key,b);
  else if(b.dataset.act=='run91') run91(b);
  else if(b.dataset.act=='stop91') stop91(b);
  else if(b.dataset.act=='save91') save91(b);
  else if(b.dataset.act=='del') delHist(b.dataset.id,b);

  else if(b.dataset.act=='clearHist') clearHist(b);
  else if(b.dataset.act=='runPdlAuto') runPdlAuto(b);
  else if(b.dataset.act=='savePdlAuto') savePdlAuto(b);
  else if(b.dataset.act=='stopPdlAuto') stopPdlAuto(b);
  else if(b.dataset.act=='togglePdlLog') togglePdlLog(b);
});
function load(){
  fetch('api/status'+location.search).then(r=>r.json()).then(d=>{
    var a=d.active||[], q=d.queued||[], h='';
    var cntTxt='';
    if(a.length) cntTxt+='('+a.length+')';
    if(q.length) cntTxt+=(cntTxt?' ':'')+'还剩'+q.length+'个排队';
    document.getElementById('cnt').textContent=cntTxt;
    if(!a.length && !q.length) h='<div class="empty">当前没有传输任务</div>';
    a.forEach(function(t){
      h+='<div class="card"><div class="row"><span class="name">'+esc(t.label)+'</span>'
        +'<span class="phase">'+esc(t.phase)+'</span></div>'
        +'<div class="bar"><i style="width:'+t.pct+'%"></i></div>'
        +'<div class="row"><div class="meta">'+t.pct+'% · '+esc(t.done)+' / '+esc(t.total)
        +(t.speed?' · '+esc(t.speed):'')+(t.eta?' · 还剩'+esc(t.eta):'')+'</div>'
        +'<button class="btn btn-sm btn-danger" data-act="cancel" data-key="'+esc(t.key)+'">取消</button></div></div>';
    });
    q.forEach(function(t){
      h+='<div class="card"><div class="row"><span class="name">'+esc(t.label||t.key)+'</span>'
        +'<span class="phase">排队中</span></div>'
        +'<button class="btn btn-sm btn-danger" data-act="cancel" data-key="'+esc(t.key)+'">取消</button></div>';
    });
    document.getElementById('active').innerHTML=h;
    var hh='';
    (d.history||[]).forEach(function(r){
      var mark=r.result=='ok'?'<span class="tag tag-ok">成功</span>':(r.result=='cancel'?'<span class="tag tag-cancel">取消</span>':'<span class="tag tag-fail">失败</span>');
      var info;
      if(r.result=='ok') info=esc(r.folder)+' · '+esc(r.size)+' · '+r.parts+'段 · 用时'+esc(r.elapsed);
      else if(r.result=='cancel') info='已取消';
      else info='失败：'+esc((r.error||'').slice(0,60));
      hh+='<div class="hist"><div class="info">'+mark+' '+esc(r.ts)+' '+info+'</div>'
        +(r.id?'<button class="btn btn-sm btn-text" data-act="del" data-id="'+esc(r.id)+'">删除</button>':'')+'</div>';
    });
    if(!hh) hh='<div class="empty">暂无记录</div>';
    document.getElementById('hist').innerHTML=hh;
    document.getElementById('updated').textContent='更新于 '+new Date().toLocaleTimeString();
  }).catch(function(){});
}
load(); var timer=setInterval(load, 5000);
load91();
loadPdlAuto(); var pdlTimer=setInterval(loadPdlAuto, 5000);
// 切到后台时暂停轮询，回来立即刷一次（省电）
document.addEventListener('visibilitychange', function(){
  if(document.hidden){ clearInterval(timer); timer=null; clearInterval(pdlTimer); pdlTimer=null; }
  else if(!timer){ load(); timer=setInterval(load, 5000); loadPdlAuto(); pdlTimer=setInterval(loadPdlAuto, 5000); }
});
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authed(self):
        # 学 t3code：token 优先从 header 取（前端从 location.hash 读，不经 URL query，不进日志）
        tok = self.headers.get('X-Token', '')
        if not tok:
            q = parse_qs(urlparse(self.path).query)
            tok = q.get('token', [''])[0]
        ip = self.client_address[0]
        if _blocked(ip):
            return False
        ok = bool(tok) and hmac.compare_digest(tok.encode(), self.server.token.encode())
        if tok and not ok:
            _FAILS.setdefault(ip, []).append(time.time())
        return ok

    def do_GET(self):
        if not self._authed():
            self.send_response(401)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write('unauthorized'.encode())
            return
        path = urlparse(self.path).path
        # 91porn API 代理到独立后端 (8898)，前端不变
        if path in ('/api/91porn_status', '/api/91porn_schedule'):
            try:
                import urllib.request
                # 8898 后端要 ?token=，8899 前端用 X-Token，代理时补上
                q = urlparse(self.path).query
                if 'token=' not in q:
                    q = (q + '&' if q else '') + 'token=' + self.server.token
                url = 'http://127.0.0.1:8898' + path + '?' + q
                data = urllib.request.urlopen(url, timeout=5).read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(('91porn backend error: %s' % e).encode())
            return
        if path == '/api/pdl_auto_status':
            try:
                import subprocess, os, time, re
                cfg = {}
                try:
                    with open('/root/91porna-dl/auto.json') as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {'hour': 3, 'minute': 0, 'enabled': True,
                           'category': 'now_month_comment', 'pages': 2, 'limit': 20}
                # 查 timer 状态和下次运行时间
                r = subprocess.run(['systemctl', 'is-active', '91porna-dl-auto.timer'],
                                   capture_output=True, text=True, timeout=5)
                cfg['enabled'] = (r.stdout.strip() == 'active')
                try:
                    r2 = subprocess.run(['systemctl', 'list-timers', '91porna-dl-auto.timer',
                                         '--no-pager'], capture_output=True, text=True, timeout=5)
                    m = re.search(r'(\w+ \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', r2.stdout)
                    if m:
                        cfg['next_run'] = m.group(1)
                except Exception:
                    pass
                # 查是否正在运行
                r2 = subprocess.run(['pgrep', '-f', '91porna-dl/main.py'],
                                    capture_output=True, text=True, timeout=5)
                cfg['running'] = bool(r2.stdout.strip())
                # 上次运行时间（看日志文件 mtime）
                try:
                    mtime = os.path.getmtime('/tmp/91porna-dl-auto.log')
                    cfg['last_run'] = time.strftime('%m-%d %H:%M', time.localtime(mtime))
                except Exception:
                    pass
                # 读抓取/下载/上传统计
                try:
                    with open('/tmp/91porna-dl-stats.json') as sf:
                        stats = json.load(sf)
                        cfg['scraped'] = stats.get('scraped', 0)
                        cfg['downloaded'] = stats.get('downloaded', 0)
                        cfg['uploaded'] = stats.get('uploaded', 0)
                        cfg['stats_updated'] = stats.get('updated', '')
                except Exception:
                    pass
                # 解析下载进度（从日志最后几行）
                try:
                    with open('/tmp/91porna-dl-auto.log', 'rb') as f:
                        f.seek(max(0, os.path.getsize('/tmp/91porna-dl-auto.log') - 2000))
                        tail = f.read().decode('utf-8', errors='ignore')
                        # 找 tqdm 进度行：如 "63%|██████▎   | 136M/217M"
                        lines = tail.strip().split('\n')
                        for line in reversed(lines):
                            m = re.search(r'(\d+)%\|.*?\| (\S+)/(\S+) \[', line)
                            if m:
                                cfg['progress'] = f"{m.group(1)}% ({m.group(2)}/{m.group(3)})"
                                break
                except Exception:
                    pass
                result = {'ok': True, 'result': cfg}
            except Exception as e:
                result = {'ok': False, 'error': str(e)[:100]}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/api/pdl_auto_log':
            try:
                import subprocess, re
                r = subprocess.run(['tail', '-50', '/tmp/91porna-dl-auto.log'],
                                   capture_output=True, text=True, timeout=5)
                log_text = re.sub(r'\x1b\[[0-9;]*m', '', r.stdout)
                result = {'ok': True, 'log': log_text[-3000:]}
            except Exception as e:
                result = {'ok': False, 'error': str(e)[:100]}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/api/status' or path == '/api/status/':
            try:
                body = json.dumps(self.server.snapshot_fn(),
                                  ensure_ascii=False).encode()
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = PAGE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        if not self._authed():
            self.send_response(401)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write('unauthorized'.encode())
            return
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            data = {}
        # 91porn API 代理到独立后端 (8898)，前端不变
        if path in ('/api/91porn_schedule', '/api/run_91porn', '/api/stop_91porn'):
            try:
                import urllib.request
                # 8898 后端要 ?token=，补上（跟 do_GET 一样）
                q = urlparse(self.path).query
                if 'token=' not in q:
                    q = (q + '&' if q else '') + 'token=' + self.server.token
                url = 'http://127.0.0.1:8898' + path + '?' + q
                req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                             headers={'Content-Type': 'application/json'})
                resp = urllib.request.urlopen(req, timeout=10).read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(('91porn backend error: %s' % e).encode())
            return
        if path == '/api/pdl_auto':
            try:
                import subprocess
                hour = int(data.get('hour', 3))
                minute = int(data.get('minute', 0))
                enabled = bool(data.get('enabled', True))
                cat = data.get('category', 'now_month_comment')
                pages = int(data.get('pages', 2) or 2)
                limit = int(data.get('limit', 20) or 20)
                if cat not in _PDL_CATS:
                    raise ValueError('非法分类')
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError('时间非法')
                pages = max(1, min(pages, 20))
                limit = max(1, min(limit, 200))
                # 保存配置
                cfg = {'hour': hour, 'minute': minute, 'enabled': enabled,
                       'category': cat, 'pages': pages, 'limit': limit}
                with open('/root/91porna-dl/auto.json', 'w') as f:
                    json.dump(cfg, f)
                # 更新 service 的 ExecStart
                svc = f"""[Unit]
Description=91porna-dl auto scrape
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/root/91porna-dl
ExecStart=/root/91porna-dl/venv/bin/python /root/91porna-dl/main.py -c {cat} -p {pages} -n {limit} -o /root/91porna-dl/downloads --concurrency 2 --browser-concurrency 2
StandardOutput=append:/tmp/91porna-dl-auto.log
StandardError=append:/tmp/91porna-dl-auto.log
"""
                with open('/etc/systemd/system/91porna-dl-auto.service', 'w') as f:
                    f.write(svc)
                # 更新 timer
                tmr = f"""[Unit]
Description=Run 91porna-dl auto scrape daily

[Timer]
OnCalendar=*-*-* {hour:02d}:{minute:02d}:00
Persistent=true

[Install]
WantedBy=timers.target
"""
                with open('/etc/systemd/system/91porna-dl-auto.timer', 'w') as f:
                    f.write(tmr)
                subprocess.run(['systemctl', 'daemon-reload'], timeout=10)
                if enabled:
                    subprocess.run(['systemctl', 'enable', '--now', '91porna-dl-auto.timer'],
                                   timeout=10, capture_output=True)
                else:
                    subprocess.run(['systemctl', 'disable', '--now', '91porna-dl-auto.timer'],
                                   timeout=10, capture_output=True)
                result = {'ok': True, 'next': f'每天 {hour:02d}:{minute:02d}'}
            except Exception as e:
                result = {'ok': False, 'error': str(e)[:200]}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/api/pdl_auto_stop':
            try:
                import subprocess
                subprocess.run(['pkill', '-f', '91porna-dl/main.py'],
                               timeout=5, capture_output=True)
                result = {'ok': True}
            except Exception as e:
                result = {'ok': False, 'error': str(e)[:100]}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/api/pdl_auto_run':
            try:
                import subprocess
                # 用保存的配置立即跑一次
                cfg = {}
                try:
                    with open('/root/91porna-dl/auto.json') as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {'category': 'now_month_comment', 'pages': 2, 'limit': 20}
                cat = cfg.get('category', 'now_month_comment')
                if cat not in _PDL_CATS:
                    raise ValueError('非法分类')
                pages = max(1, min(int(cfg.get('pages', 2) or 2), 20))
                limit = max(1, min(int(cfg.get('limit', 20) or 20), 200))
                cmd = ['/root/91porna-dl/venv/bin/python', '/root/91porna-dl/main.py',
                       '-c', cat, '-p', str(pages), '-n', str(limit),
                       '-o', '/root/91porna-dl/downloads']
                logf = open('/tmp/91porna-dl-auto.log', 'a')
                subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                 start_new_session=True)
                result = {'ok': True}
            except Exception as e:
                result = {'ok': False, 'error': str(e)[:200]}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        ok = False
        if path == '/api/cancel':
            fn = getattr(self.server, 'cancel_fn', None)
            if fn:
                try:
                    ok = bool(fn(data.get('key', '')))
                except Exception:
                    ok = False
        elif path == '/api/del_history':
            fn = getattr(self.server, 'del_history_fn', None)
            if fn:
                try:
                    ok = bool(fn(data.get('id', '')))
                except Exception:
                    ok = False
        elif path == '/api/clear_history':
            fn = getattr(self.server, 'clear_history_fn', None)
            if fn:
                try:
                    ok = bool(fn())
                except Exception:
                    ok = False
        body = json.dumps({'ok': ok}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)



def start_dashboard(port, token, snapshot_fn, cancel_fn=None,
                    del_history_fn=None, clear_history_fn=None, host='0.0.0.0'):
    """Start the dashboard in a daemon thread. Returns the server."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.token = token
    server.snapshot_fn = snapshot_fn
    server.cancel_fn = cancel_fn
    server.del_history_fn = del_history_fn
    server.clear_history_fn = clear_history_fn
    t = threading.Thread(target=server.serve_forever,
                         name='dashboard', daemon=True)
    t.start()
    print('DASHBOARD on :%d' % port, flush=True)
    return server
