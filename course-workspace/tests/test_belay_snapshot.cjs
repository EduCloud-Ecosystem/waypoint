const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const file = path.join(__dirname, '../course/labextension-belay/static/remoteEntry.js');
function loaded(extra = {}) {
  const context = {window:{}, module:{exports:{}}, TextEncoder, ...extra};
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), context);
  return context;
}
function notebook(source = 'print(1)', dirty = true, type = 'code') {
  const panel = {content:{activeCell:{model:{type, sharedModel:{getSource:() => source}}}}, context:{model:{dirty}}};
  return {panel, app:{shell:{currentWidget:panel}}, tracker:{currentWidget:panel}};
}
test('actual active code only; no Markdown, outputs, paths or metadata', () => {
  const {capture} = loaded().module.exports;
  const {panel,app,tracker} = notebook();
  Object.defineProperty(panel, 'metadata', {get(){throw Error('must not read metadata');}});
  Object.defineProperty(panel.content.activeCell.model, 'outputs', {get(){throw Error('must not read outputs');}});
  const result = capture(app,tracker);
  assert.equal(result.source,'print(1)'); assert.equal(result.dirty,true);
  assert.deepEqual(Object.keys(result).sort(),['dirty','source']);
  assert.ok(Object.isFrozen(result));
  panel.content.activeCell.model.sharedModel.getSource = () => 'later edit';
  panel.context.model.dirty = false;
  assert.equal(result.source,'print(1)'); assert.equal(result.dirty,true);
});
test('reject other active document, non-code, invalid/empty and oversize UTF8', () => {
  const {capture} = loaded().module.exports;
  for (const [source,dirty,type] of [['x',true,'markdown'],['',false,'code'],[' ',false,'code'],
      ['x',null,'code'], ['x'.repeat(8193),false,'code'],['😀'.repeat(2049),false,'code']]) {
    const {app,tracker} = notebook(source,dirty,type);
    assert.throws(() => capture(app,tracker));
  }
  const {app,tracker} = notebook('😀'.repeat(2048),false);
  assert.equal(capture(app,tracker).dirty,false);
  app.shell.currentWidget = {};
  assert.throws(() => capture(app,tracker));
});
test('handshake requires exact popup/origin/nonce/version/type/fields', () => {
  const {ready} = loaded().module.exports;
  const popup = {}, origin='http://127.0.0.1:5190', nonce='a'.repeat(64);
  const event = {source:popup,origin,data:{type:'belay-notebook-ready',version:1,nonce}};
  assert.equal(ready(event,popup,origin,nonce),true);
  for (const wrong of [{...event,source:{}},{...event,origin:'http://127.0.0.1:5191'},
      {...event,data:{...event.data,nonce:'b'.repeat(64)}}, {...event,data:{...event.data,version:2}},
      {...event,data:{...event.data,type:'other'}}, {...event,data:{...event.data,source:'extra'}}])
    assert.equal(ready(wrong,popup,origin,nonce),false);
});
test('opt-in activation does not open/send/save/Ask; explicit action freezes one snapshot', async () => {
  const {panel,app,tracker} = notebook('actual unsaved cell',true);
  const elements = [], listeners = new Map(), posted = [], opened = [];
  const popup = {postMessage:(body,origin) => posted.push({body,origin})};
  const window = {_JUPYTERLAB:{},open:(...args) => {opened.push(args); return popup;},
    addEventListener:(name,fn) => listeners.set(name,fn), removeEventListener:(name,fn) => {if(listeners.get(name)===fn)listeners.delete(name);}};
  const document = {cookie:'_xsrf=native-cookie', getElementById:() => ({textContent:'{"baseUrl":"/"}'}),
    createElement:() => {const node={setAttribute(){},append(){},style:{},addEventListener(){}}; elements.push(node);return node;}};
  const commands = new Map(); app.commands = {addCommand:(name,command) => commands.set(name,command)};
  app.shell.add = () => {};
  let fetchCount = 0;
  const context = loaded({window,document,location:{origin:'http://127.0.0.1:5190'},
    crypto:{getRandomValues:buffer => buffer.fill(1)}, Uint8Array,
    setTimeout:() => 1, clearTimeout(){},
    fetch:async (url,options) => {fetchCount++; assert.equal(url,'/belay/config');
      assert.equal(options.headers['X-XSRFToken'],'native-cookie');
      return {ok:true,json:async()=>({version:1,base:'/belay',max_source_bytes:8192})};}});
  const remote = context.window._JUPYTERLAB['@educloud/waypoint-belay'];
  remote.init({'@jupyterlab/notebook':{'4.6.0':{get:async()=>()=>({INotebookTracker:{}})}},
    '@lumino/widgets':{'2.8.0':{get:async()=>()=>({Widget:class {}})}}});
  const plugin = (await remote.get('./extension'))().default;
  await plugin.activate(app,tracker);
  assert.equal(fetchCount,1); assert.equal(opened.length,0); assert.equal(posted.length,0);
  commands.get('waypoint-belay:ask-active-code').execute();
  assert.equal(opened.length,1); assert.match(opened[0][0],/^\/belay\/#belay-notebook=[a-f0-9]{64}$/);
  panel.content.activeCell.model.sharedModel.getSource = () => 'changed after capture';
  const nonce = opened[0][0].split('=')[1];
  const receive = listeners.get('message');
  receive({source:{},origin:'http://127.0.0.1:5190',data:{type:'belay-notebook-ready',version:1,nonce}});
  assert.equal(posted.length,0);
  receive({source:popup,origin:'http://127.0.0.1:5190',data:{type:'belay-notebook-ready',version:1,nonce}});
  assert.equal(posted.length,1); assert.equal(posted[0].body.source,'actual unsaved cell');
  assert.equal(posted[0].body.dirty,true); assert.equal(posted[0].origin,'http://127.0.0.1:5190');
  assert.deepEqual(Object.keys(posted[0].body).sort(),['dirty','nonce','source','type','version']);
  assert.equal(listeners.has('message'),false); assert.equal(fetchCount,1);
});

test('native panel reuses one frame, authenticates messages and protects a pending hint', () => {
  const listeners = new Map(), frames = [], sent = [], added = [], activated = [], collapsed = [];
  const origin = 'http://127.0.0.1:5190';
  const {app,tracker} = notebook(); tracker.currentWidget.id = 'my-notebook';
  app.shell.add = (...args) => added.push(args);
  app.shell.activateById = id => activated.push(id);
  app.shell.collapseLeft = () => collapsed.push('left');
  app.shell.collapseRight = () => collapsed.push('right');
  const document = {createElement(tag) {
    const node = {append(){}, addEventListener(){}, focus(){}};
    node.contentDocument = {getElementById:() => null};
    if (tag === 'iframe') {node.contentWindow = {postMessage:(body,target)=>sent.push({body,target})}; frames.push(node);}
    return node;
  }};
  class Widget {constructor({node}) {this.node=node; this.title={};}}
  const {panelBridge} = loaded({document,location:{origin},
    window:{addEventListener:(name,fn)=>listeners.set(name,fn)},
    crypto:{getRandomValues:b=>b.fill(2)},Uint8Array}).module.exports;
  const status = {};
  const show = panelBridge(app,tracker,Widget,'/belay/',status,{matches:true});
  assert.equal(frames.length,0); assert.equal(sent.length,0);
  show(Object.freeze({source:'selected code',dirty:true}));
  assert.equal(frames.length,1); assert.equal(added[0][1],'right'); assert.equal(sent.length,0);
  assert.deepEqual(collapsed,['left']);
  const frame = frames[0], nonce = frame.src.split('=')[1], receive = listeners.get('message');
  const event = (type,fields={}) => ({source:frame.contentWindow,origin,data:{type,version:1,nonce,...fields}});
  for (const bad of [{...event('belay-notebook-ready'),origin:'https://evil.invalid'},
    {...event('belay-notebook-ready'),source:{}},event('belay-notebook-ready',{nonce:'bad'}),
    event('belay-notebook-ready',{extra:true})]) receive(bad);
  assert.equal(sent.length,0);
  receive(event('belay-notebook-ready'));
  assert.equal(sent[0].body.source,'selected code'); assert.equal(sent[0].target,origin);
  receive(event('belay-notebook-state',{busy:true}));
  show({source:'must not replace pending',dirty:false}); assert.equal(sent.length,1);
  assert.match(status.textContent,/pending/);
  receive(event('belay-notebook-state',{busy:false,extra:true}));
  show({source:'still pending',dirty:false}); assert.equal(sent.length,1);
  receive(event('belay-notebook-state',{busy:false}));
  show({source:'new reviewed selection',dirty:false});
  assert.equal(sent.length,2); assert.equal(frames.length,1); assert.equal(added.length,1);
  assert.deepEqual(Object.keys(sent[1].body).sort(),['dirty','nonce','source','type','version']);
  receive(event('belay-notebook-return')); assert.equal(activated.at(-1),'my-notebook');
  assert.equal(collapsed.at(-1),'right');
  receive(event('belay-notebook-captured',{accepted:false})); assert.match(status.textContent,/pending/);
});
