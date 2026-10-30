"use strict";
// Device preferences affect presentation only, never server generation settings.
window.WorkbenchLayout = (() => {
  const key = "workbench-layout-v1";
  let preferences = {zoom:100, sidebar:false, folds:{}};
  try { preferences = {...preferences, ...JSON.parse(localStorage.getItem(key)||"{}")}; } catch {}
  const persist = () => localStorage.setItem(key, JSON.stringify(preferences));
  function zoom(value, save=true) {
    const number=Number(value);
    preferences.zoom=Number.isFinite(number)?Math.max(50,Math.min(200,Math.round(number))):100;
    document.documentElement.style.setProperty("--ui-zoom",String(preferences.zoom/100));
    document.body.style.zoom=String(preferences.zoom/100);
    for(const node of document.querySelectorAll('[data-ui-zoom]'))node.value=String(preferences.zoom);
    for(const node of document.querySelectorAll('[data-zoom-value]'))node.textContent=preferences.zoom+'%';
    if(save)persist();
    window.dispatchEvent(new Event('resize'));
    return preferences.zoom;
  }
  function sidebar(value, save=true) {
    preferences.sidebar=!!value;
    document.documentElement.classList.toggle('sidebar-collapsed',preferences.sidebar);
    const button=document.querySelector('#sidebar-collapse');
    if(button){button.textContent=preferences.sidebar?'☰':'‹';button.title=preferences.sidebar?'展开左侧栏':'收起左侧栏';button.setAttribute('aria-label',button.title);button.setAttribute('aria-expanded',String(!preferences.sidebar));}
    if(save)persist();
  }
  function fold(id,label) {
    const el=document.getElementById(id);if(!el||el.parentElement?.classList.contains('chat-fold'))return;
    const panel=document.createElement('section');panel.className='chat-fold';panel.dataset.foldId=id;
    const button=document.createElement('button');button.className='chat-fold-toggle';button.type='button';button.dataset.foldToggle=id;
    button.setAttribute('aria-controls',id);el.before(panel);panel.append(button,el);
    if(id==='tool-toggles'){const guide=document.querySelector('.beginner-guide');if(guide)panel.append(guide);}
    const apply=()=>{const closed=!!preferences.folds[id];panel.classList.toggle('is-collapsed',closed);button.textContent=(closed?'▸ ':'▾ ')+label;button.setAttribute('aria-expanded',String(!closed));};
    button.onclick=()=>{preferences.folds[id]=!preferences.folds[id];persist();apply();};apply();
  }
  function boot() {
    zoom(preferences.zoom,false);sidebar(preferences.sidebar,false);
    const bar=document.querySelector('.topbar');
    if(!document.querySelector('#sidebar-collapse')){const b=document.createElement('button');b.id='sidebar-collapse';b.className='icon-btn sidebar-collapse';bar.prepend(b);b.onclick=()=>sidebar(!preferences.sidebar);sidebar(preferences.sidebar,false);}
    for(const node of document.querySelectorAll('#nav [data-view]'))node.title=node.textContent.trim();
    document.querySelector('#new-chat')?.setAttribute('title','新建对话');
    fold('model-runtime','模型动态状态');fold('tool-toggles','工具与附件能力');fold('knowledge-bar','知识库与记忆');
  }
  function settings(anchor) {
    const section=document.createElement('section');section.className='settings-section card';
    section.innerHTML='<h2>界面大小与折叠</h2><p class="field-note">此设备记住一套外观偏好，远程电脑可使用不同大小。缩放不改变模型参数。</p><div class="zoom-control"><label>整体大小 <b data-zoom-value></b><input data-ui-zoom type="range" min="50" max="200" step="5" aria-label="全局界面缩放"></label><select id="ui-zoom-select" aria-label="缩放比例"><option value="50">50%</option><option value="75">75%</option><option value="100">100%</option><option value="125">125%</option><option value="150">150%</option><option value="175">175%</option><option value="200">200%</option></select><button id="reset-ui-zoom" class="button">恢复100%</button></div><p class="settings-help">左侧栏可收起为图标；聊天的模型状态、工具、知识库条可分别收起。刷新后保持。</p>';
    anchor.before(section);
    const slider=section.querySelector('[data-ui-zoom]'),select=section.querySelector('select');
    slider.oninput=e=>{e.stopPropagation();zoom(slider.value);select.value=String(preferences.zoom);};
    select.onchange=e=>{e.stopPropagation();zoom(select.value);};section.querySelector('#reset-ui-zoom').onclick=()=>{zoom(100);select.value='100';};
    zoom(preferences.zoom,false);select.value=String(preferences.zoom);
  }
  return Object.freeze({boot,settings,zoom,sidebar,preferences:()=>JSON.parse(JSON.stringify(preferences))});
})();
