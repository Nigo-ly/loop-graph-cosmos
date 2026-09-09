import { it } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import os from 'node:os';

function luminance(rgb) {
  const values=rgb.match(/[\d.]+/g).slice(0,3).map(Number).map(n=>n/255).map(n=>n<=0.04045?n/12.92:((n+0.055)/1.055)**2.4);
  return values[0]*0.2126+values[1]*0.7152+values[2]*0.0722;
}
function contrast(a,b) {const x=luminance(a),y=luminance(b);return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05);}

it('readable workbench has physical text sizes, contrast and responsive records without artificial zoom', async t=>{
  const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  if(!existsSync(chrome))return t.skip('installed Chromium required; no dependency downloaded');
  const css=readFileSync('src/runtime/styles.css','utf8')+'\n'+readFileSync('src/console/console.css','utf8');
  const dir=mkdtempSync(path.join(os.tmpdir(),'readable-workbench-'));
  try {
    const html=`<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><style>body{margin:0}${css} button{background:#fff;color:#fff}.setting-item-name{color:#111}button.mod-cta{background:#7050b0;color:#fff}.modal-bg{background:rgba(255,255,255,.6);opacity:.8}</style>
      <main class="my-life-homepage-view"><div class="life-home"><section class="life-cosmos-home">
        <header class="life-cosmos-topbar"><div class="life-cosmos-brand"><strong>My Life</strong></div><nav class="life-cosmos-nav"><button class="is-active">工作台</button><button>研究记录</button><button>知识库</button></nav></header>
        <div class="life-cosmos-capsule"><button class="life-cosmos-capsule-action">记录碎片</button><button class="life-cosmos-capsule-action">刷新</button></div>
        <div class="life-workbench-head"><h1>工作台</h1><p>记下链接与问题，查看研究结论，积累可复用的知识。</p></div>
        <div class="life-workbench"><div class="life-workbench-stats">${Array.from({length:4},(_,i)=>'<button class="life-workbench-stat"><strong>'+i+'</strong><span>可用研究结论</span></button>').join('')}</div>
        <section class="life-workbench-section"><h2>当前进展</h2><p id="body-copy">正文需要持续阅读，使用真实字号和舒适行距。</p><small id="secondary-copy">仅已读取来源支持的范围。</small></section></div>
        <div class="life-frag-panorama-toolbar"><input class="life-frag-panorama-search" placeholder="搜索碎片标题…"></div>
        <div class="life-frag-panorama life-research-list"><div class="life-frag-panorama-rows"><button class="life-frag-panorama-row life-research-row is-done"><div class="life-frag-panorama-identity"><b class="life-research-row-title">${'MarkupSafe 是否适合安全显示网页摘录？'.repeat(4)}</b><small>2026-09-09</small></div><div class="life-frag-panorama-next"><strong class="life-research-row-status">结论已自动沉淀</strong><p class="life-research-row-next">${'完整结论保留了适用条件与限制，并提供可追溯证据。'.repeat(6)}</p><small>查看结果 →</small></div></button></div></div>
        <details class="life-cosmos-tools"><summary>更多工具与系统管理</summary><div class="life-cosmos-tool-links"><button>系统管理</button><div class="life-cosmos-svc-dots"><button class="life-cosmos-svc-dot">Graph 运行状态</button><button class="life-cosmos-svc-dot" data-state="attention">Loop 待确认</button></div></div></details>
        <div class="life-cosmos-panel" id="panel"><p>知识目录</p><div class="life-knowledge-controls"><button>搜索</button><button>刷新目录</button></div></div>
      </section></div></main><div class="modal-container" id="capture-container"><div class="modal-bg"></div><div class="modal my-life-quick-capture-modal"><div class="setting-item-name">内容</div><div class="setting-item-description">问题与公开资料会发送给 K3。</div><textarea></textarea><button class="mod-cta">保存</button></div></div><div class="modal-container" id="other-container"><div class="modal-bg"></div><div class="modal">其他弹窗</div></div><output id="metrics"></output>
      <script>const q=s=>document.querySelector(s),s=el=>getComputedStyle(el),r=el=>el.getBoundingClientRect();const home=q('.life-cosmos-home'),row=q('.life-research-row'),body=q('#body-copy'),secondary=q('#secondary-copy'),panel=q('#panel'),nav=q('.life-cosmos-nav button'),title=q('.life-research-row-title'),next=q('.life-research-row-next'),stat=q('.life-workbench-stat'),status=q('.life-research-row-status'),input=q('.my-life-quick-capture-modal textarea');q('.life-cosmos-tools').open=true;nav.focus();
      q('#metrics').textContent=JSON.stringify({viewport:innerWidth,homeWidth:r(home).width,homeRight:r(home).right,homeLeft:r(home).left,homeZoom:s(home).zoom,bodyFont:s(body).fontSize,bodyLine:s(body).lineHeight,secondaryFont:s(secondary).fontSize,titleFont:s(title).fontSize,nextFont:s(next).fontSize,bodyColor:s(body).color,secondaryColor:s(secondary).color,bg:s(home).backgroundColor,panel:s(panel).backgroundColor,panelImage:s(panel).backgroundImage,panelShadow:s(panel).boxShadow,panelFilter:s(panel).backdropFilter,navFont:s(nav).fontSize,navHeight:r(nav).height,focusOutline:s(nav).outlineWidth,rowWidth:r(row).width,rowClient:row.clientWidth,rowScroll:row.scrollWidth,rowRight:r(row).right,rowLeft:r(row).left,titleClamp:s(title).webkitLineClamp,statsHeight:r(stat).height,statFont:s(stat.querySelector('span')).fontSize,statusFont:s(status).fontSize,knowledgeButtonColor:s(q('.life-knowledge-controls button')).color,knowledgeButtonBg:s(q('.life-knowledge-controls button')).backgroundColor,captureBackdrop:s(q('#capture-container .modal-bg')).backgroundColor,captureBackdropOpacity:s(q('#capture-container .modal-bg')).opacity,otherBackdrop:s(q('#other-container .modal-bg')).backgroundColor,otherBackdropOpacity:s(q('#other-container .modal-bg')).opacity,captureNameColor:s(q('.setting-item-name')).color,captureButtonColor:s(q('.my-life-quick-capture-modal .mod-cta')).color,captureButtonBg:s(q('.my-life-quick-capture-modal .mod-cta')).backgroundColor,inputFont:s(input).fontSize,inputHeight:r(input).height,serviceWidth:r(q('.life-cosmos-svc-dot')).width,serviceClient:q('.life-cosmos-svc-dot').clientWidth,serviceScroll:q('.life-cosmos-svc-dot').scrollWidth,pageScroll:document.documentElement.scrollWidth,animations:document.getAnimations().map(a=>a.animationName)});</script>`;
    writeFileSync(path.join(dir,'page.html'),html);
    for(const width of [520,1224,2400]) {
      const stdout=await new Promise((resolve,reject)=>{
        const child=spawn(chrome,['--headless=new','--disable-gpu','--disable-background-networking','--disable-extensions','--disable-component-update','--no-first-run','--no-default-browser-check',`--user-data-dir=${path.join(dir,'profile-'+width)}`,`--window-size=${width},900`,'--force-device-scale-factor=1','--dump-dom','file://'+path.join(dir,'page.html')],{detached:true,stdio:['ignore','pipe','ignore']});
        let output='',finished=false;
        const finish=error=>{if(finished)return;finished=true;clearTimeout(timer);try{process.kill(-child.pid,'SIGTERM');}catch{}if(error)reject(error);else resolve(output);};
        const timer=setTimeout(()=>finish(new Error('isolated Chromium did not report geometry')),20000);
        child.stdout.setEncoding('utf8');child.stdout.on('data',chunk=>{output+=chunk;if(/<output id="metrics">\{.*?\}<\/output>/s.test(output))finish();});child.on('error',finish);child.on('exit',code=>{if(!finished)finish(new Error('Chromium exited before geometry: '+code));});
      });
      const metrics=JSON.parse(stdout.match(/<output id="metrics">(.*?)<\/output>/s)[1]);
      const ratios={body:contrast(metrics.bodyColor,metrics.panel),secondary:contrast(metrics.secondaryColor,metrics.panel)};
      t.diagnostic(JSON.stringify({width,...metrics,contrast:ratios}));
      assert.equal(metrics.bg,'rgb(24, 27, 32)');assert.equal(metrics.panel,'rgb(34, 39, 46)');
      assert.equal(metrics.panelImage,'none');assert.equal(metrics.panelShadow,'none');assert.equal(metrics.panelFilter,'none');
      assert.equal(metrics.homeZoom,'1');assert.ok(metrics.homeWidth<=1161);assert.ok(metrics.homeLeft>=-1&&metrics.homeRight<=width+1);
      assert.equal(metrics.bodyFont,'16px');assert.ok(Math.abs(parseFloat(metrics.bodyLine)-26.4)<0.1);
      for(const value of [metrics.secondaryFont,metrics.statFont,metrics.statusFont])assert.ok(parseFloat(value)>=14);
      assert.ok(parseFloat(metrics.titleFont)>=18);assert.equal(metrics.nextFont,'16px');
      assert.ok(ratios.body>=7);assert.ok(ratios.secondary>=4.5);
      assert.ok(metrics.navHeight>=42);assert.ok(parseFloat(metrics.focusOutline)>=2);assert.ok(metrics.statsHeight>=96);
      assert.ok(metrics.rowClient>=metrics.rowScroll-1&&metrics.rowRight<=width+1&&metrics.rowLeft>=-1);assert.equal(metrics.titleClamp,'none');
      assert.equal(metrics.knowledgeButtonColor,'rgb(240, 243, 246)');assert.equal(metrics.knowledgeButtonBg,'rgb(34, 39, 46)');
      assert.equal(metrics.captureBackdrop,'rgba(0, 0, 0, 0.6)');assert.equal(metrics.captureBackdropOpacity,'1');assert.equal(metrics.otherBackdrop,'rgba(255, 255, 255, 0.6)');assert.equal(metrics.otherBackdropOpacity,'0.8');
      assert.equal(metrics.captureNameColor,'rgb(240, 243, 246)');assert.equal(metrics.captureButtonBg,'rgb(146, 197, 223)');assert.equal(metrics.captureButtonColor,'rgb(24, 27, 32)');
      assert.equal(metrics.inputFont,'16px');assert.ok(metrics.inputHeight>=240);assert.ok(metrics.pageScroll<=width+1);
      assert.ok(metrics.serviceWidth>=120 && metrics.serviceScroll<=metrics.serviceClient+1,'service status is a readable button, not stretched dots');
      assert.deepEqual(metrics.animations,[],'daily workbench must be static, including inherited baseline motion');
    }
  } finally {rmSync(dir,{recursive:true,force:true,maxRetries:10,retryDelay:100});}
});
