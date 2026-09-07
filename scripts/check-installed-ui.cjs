// Targeted installed-app check. No credentials are read or printed.
const pending = new Map();
let seq = 0;
(async () => {
  const pages = await (await fetch('http://127.0.0.1:9237/json/list')).json();
  const page = pages.find(p => p.type === 'page' && p.title === '墨流 InkFlow');
  if (!page) throw Error('InkFlow page unavailable');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(resolve => ws.addEventListener('open', resolve, {once:true}));
  ws.addEventListener('message', event => {
    const msg = JSON.parse(event.data);
    if (pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  const evaluate = async expression => {
    const id = ++seq;
    const response = new Promise(resolve => pending.set(id, resolve));
    ws.send(JSON.stringify({id, method:'Runtime.evaluate', params:{expression, awaitPromise:true, returnByValue:true}}));
    const value = await response;
    if (value.result?.exceptionDetails) throw Error(JSON.stringify(value.result.exceptionDetails));
    return value.result?.result?.value;
  };
  console.log(await evaluate(`(() => { const b=[...document.querySelectorAll('button')].find(b=>b.textContent==='设置'); b.click(); return 'opened settings'; })()`));
  await new Promise(r=>setTimeout(r,300));
  console.log(await evaluate(`(() => { [...document.querySelectorAll('button')].find(b=>b.textContent==='仅保存').click(); return 'clicked save'; })()`));
  await new Promise(r=>setTimeout(r,3000));
  console.log('SAVE:', await evaluate(`document.querySelector('.form-success')?.textContent || document.querySelector('.form-error')?.textContent || document.querySelector('.stage-note')?.textContent`));
  console.log(await evaluate(`(() => { [...document.querySelectorAll('button')].find(b=>b.textContent==='保存并测试连接').click(); return 'clicked model check'; })()`));
  await new Promise(r=>setTimeout(r,20000));
  console.log('CONNECTION:', await evaluate(`document.querySelector('.form-success')?.textContent || document.querySelector('.form-error')?.textContent || document.querySelector('.stage-note')?.textContent`));
  await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='关闭').click()`);
  await new Promise(r=>setTimeout(r,200));
  await evaluate(`(() => { const box=document.querySelector('.composer textarea'); const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set; setter.call(box,'你好，请用一句话回答你能怎样帮助我写小说。只聊天，不写文件。'); box.dispatchEvent(new Event('input',{bubbles:true})); })()`);
  await new Promise(r=>setTimeout(r,200));
  await evaluate(`document.querySelector('.composer').requestSubmit()`);
  await new Promise(r=>setTimeout(r,30000));
  console.log('CHAT:', await evaluate(`[...document.querySelectorAll('.messages .message')].slice(-2).map(e=>e.textContent)`));
  await evaluate(`window.__inkflowEvents=0; window.__inkflowUnsubscribe=window.inkflow.onEvent(()=>window.__inkflowEvents++);`);
  await new Promise(r=>setTimeout(r,3000));
  console.log('IDLE_EVENTS:', await evaluate(`window.__inkflowUnsubscribe(); window.__inkflowEvents`));
  ws.close();
})().catch(e=>{ console.error(e.message); process.exitCode=1; });
