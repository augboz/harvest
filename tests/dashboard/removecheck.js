const fs=require('fs'),{JSDOM}=require('jsdom');
const dom=new JSDOM(fs.readFileSync(process.argv[2],'utf8'),{runScripts:'dangerously',pretendToBeVisual:true,
 beforeParse(w){const calls=[];const chain=()=>{const o=new Proxy({},{get:(t,k)=>{
   if(typeof k==='string'&&k[0]==='_')return t[k];
   if(k==='withSuccessHandler'||k==='withFailureHandler')return fn=>{t['_'+k]=fn;return o;};
   return (...a)=>{calls.push({fn:String(k),args:a,o});};}});return o;};
  w.google={script:{}};Object.defineProperty(w.google.script,'run',{get:chain});
  w.__calls=calls;
  w.addEventListener('error',e=>console.log('ERROR:',e.error&&e.error.message));}});
const w=dom.window,doc=w.document;
setTimeout(()=>{
  let fails=0;
  const check=(l,g,x)=>{const ok=g===x;if(!ok)fails++;
    console.log((ok?'PASS  ':'FAIL  ')+l+'  got='+JSON.stringify(g)+(ok?'':' want='+JSON.stringify(x)));};
  const run=(tick)=>{
    w.__calls.length=0;
    const shell=doc.querySelector('#list .shell');
    shell.querySelector('.killx').dispatchEvent(new w.MouseEvent('click',{bubbles:true}));
    doc.getElementById('whyRep').checked=tick;
    doc.getElementById('whyGo').dispatchEvent(new w.MouseEvent('click',{bubbles:true}));
    return w.__calls.find(c=>c.fn==='removeJob');
  };
  check('unticked sends wantReplacement=false', run(false).args[1], false);
  check('ticked sends wantReplacement=true', run(true).args[1], true);
  // a tick must not carry over to the next removal, or the surprise returns
  const shell = doc.querySelector('#list .shell');
  shell.querySelector('.killx').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
  check('reopening the dialog resets the tick', doc.getElementById('whyRep').checked, false);
  console.log(fails?`\n${fails} FAILURE(S)`:'\nremoval choice verified in the DOM');
  process.exit(fails?1:0);
},400);
