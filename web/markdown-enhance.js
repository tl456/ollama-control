/* Offline, bounded Markdown cache and opt-in diagram enhancement. */
"use strict";
(() => {
  const cache = new Map();
  const escape = s => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  window.workMarkdown = function(text) {
    text = text || '';
    if(cache.has(text)) return cache.get(text);
    if(!window.marked || !window.DOMPurify) return '<p>'+escape(text).replace(/\n/g,'<br>')+'</p>';
    const code = [];
    let source = text.replace(/(```[^\n]*\n[\s\S]*?(?:```|$)|`[^`\n]+`)/g,m=>`WORKCODETOKEN${code.push(m)-1}END`);
    const math = [];
    source = source.replace(/\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([^\n]+?)\\\)|(?<!\\)\$([^\n$]+?)\$(?!\$)/g,(m,a,b,c,d)=>{
      const i=math.push({text:a??b??c??d,display:a!=null||b!=null})-1;
      return `WORKMATHTOKEN${i}END`;
    });
    const notes=[];
    source=source.replace(/^\[\^([\w-]+)\]:\s*(.+)$/gm,(m,id,note)=>{notes.push({id,note});return '';});
    source=source.replace(/\[\^([\w-]+)\]/g,(m,id)=>notes.some(n=>n.id===id)?`<sup class="md-note-ref">[${id}]</sup>`:m);
    source=source.replace(/^(\s*[-*+] )\[([ xX])\]/gm,(m,p,x)=>p+(x===' '?'☐':'☑'));
    source=source.replace(/WORKCODETOKEN(\d+)END/g,(m,i)=>code[Number(i)]);
    let html=marked.parse(source,{breaks:true,gfm:true});
    html=html.replace(/WORKMATHTOKEN(\d+)END/g,(m,i)=>`<span class="math-source" data-display="${math[i].display?'block':'inline'}" data-math="${escape(math[i].text)}">${escape(math[i].text)}</span>`);
    if(notes.length)html+='<section class="md-footnotes"><hr>'+notes.map(n=>`<p><sup>[${escape(n.id)}]</sup> ${escape(n.note)}</p>`).join('')+'</section>';
    html=DOMPurify.sanitize(html,{FORBID_TAGS:['img','style','script','iframe','input','form','object','embed'],FORBID_ATTR:['style']});
    if(text.length<500000){cache.set(text,html);if(cache.size>128)cache.delete(cache.keys().next().value);}
    return html;
  };
  let diagramModule;
  let diagramNumber=0;
  window.enhanceWorkMarkdown = async function(root, diagrams=true) {
    if(!root?.isConnected)return;
    if(window.hljs)root.querySelectorAll('pre code:not(.language-mermaid):not([data-highlighted])').forEach(code=>{
      if(code.textContent.length<100000)try{hljs.highlightElement(code);}catch{}
    });
    if(window.katex)root.querySelectorAll('.math-source:not([data-rendered])').forEach(el=>{
      el.dataset.rendered='true';
      try{katex.render(el.dataset.math,el,{displayMode:el.dataset.display==='block',throwOnError:false,trust:false,maxExpand:1000,maxSize:20});}catch{el.textContent=el.dataset.math;}
    });
    const codes=[...root.querySelectorAll('pre code.language-mermaid:not([data-diagram])')];
    if(!diagrams||!codes.length)return;
    try {
      diagramModule??=import('/assets/vendor/mermaid/mermaid.esm.min.mjs');
      const {default:mermaid}=await diagramModule;
      mermaid.initialize({startOnLoad:false,securityLevel:'strict',theme:'dark',htmlLabels:false,maxTextSize:20000,maxEdges:200,flowchart:{htmlLabels:false},suppressErrorRendering:true});
      for(const code of codes){
        code.dataset.diagram='true';
        if(code.textContent.length>20000)continue;
        try{
          const {svg}=await mermaid.render('work-diagram-'+(++diagramNumber),code.textContent);
          const panel=document.createElement('div');panel.className='md-diagram';
          panel.innerHTML=DOMPurify.sanitize(svg,{USE_PROFILES:{svg:true,svgFilters:true},FORBID_TAGS:['script','foreignObject','image','a']});
          if(code.isConnected)code.closest('pre').after(panel);
        }catch{ /* Keep the original source readable on parse failure. */ }
      }
    }catch{}
  };
})();
