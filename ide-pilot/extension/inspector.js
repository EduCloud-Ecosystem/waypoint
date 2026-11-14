// SPDX-License-Identifier: Apache-2.0
const vscode = require('vscode');
const crypto = require('crypto');

// Read the same Debug Adapter Protocol session used by the native debugger.
// Values remain in memory and are inserted as text, never interpreted as HTML.
function registerInspector(context) {
  const stopped = new Map();
  let panel, previousEditor, revision = 0, followStep = false;
  async function refresh() {
    if (!panel) return;
    const currentPanel = panel;
    const token = ++revision;
    const session = vscode.debug.activeDebugSession;
    const threadId = session && stopped.get(session.id);
    let result = { status: 'Pause at a breakpoint to inspect values.', variables: [] };
    try {
      if (threadId !== undefined) {
        const stack = await session.customRequest('stackTrace', { threadId, startFrame: 0, levels: 1 });
        const frame = stack.stackFrames?.[0];
        if (!frame) throw Error('No stack frame is available.');
        const scopes = await session.customRequest('scopes', { frameId: frame.id });
        result = { status: `Paused: ${frame.name}, line ${frame.line}`, variables: [] };
        const available = scopes.scopes.filter(scope => !scope.expensive);
        const local = available.find(scope => /local/i.test(scope.name)) || available[0];
        for (const scope of local ? [local] : []) {
          const response = await session.customRequest('variables', { variablesReference: scope.variablesReference, start: 0, count: 100 });
          for (const variable of response.variables.slice(0, 100)) {
            result.variables.push({
              name: `${scope.name}: ${variable.name}`,
              value: String(variable.value).slice(0, 4000),
            });
          }
        }
        result.note = 'Top stack frame; up to 100 values from the first local or non-expensive scope. Use the native debugger for nested objects, watches and other frames.';
      }
    } catch (error) {
      result = { status: 'Unable to inspect values: ' + error.message, variables: [] };
    }
    if (panel === currentPanel && token === revision) {
      await panel.webview.postMessage(result);
      if (followStep && threadId !== undefined) {
        panel.reveal(panel.viewColumn);
        followStep = false;
      }
    }
  }
  context.subscriptions.push(vscode.debug.registerDebugAdapterTrackerFactory('*', {
    createDebugAdapterTracker(session) {
      return { onDidSendMessage(message) {
        if (message.type !== 'event') return;
        if (message.event === 'stopped' && message.body?.threadId !== undefined) {
          stopped.set(session.id, message.body.threadId);
          void refresh();
        } else if (message.event === 'continued' || message.event === 'terminated') {
          stopped.delete(session.id);
          void refresh();
        }
      } };
    },
  }));
  context.subscriptions.push(vscode.debug.onDidTerminateDebugSession(session => {
    stopped.delete(session.id);
    void refresh();
  }));
  context.subscriptions.push(vscode.commands.registerCommand('waypoint.inspect', async () => {
    await vscode.commands.executeCommand('workbench.action.closeSidebar');
    await vscode.commands.executeCommand('workbench.action.closePanel');
    if (panel) { panel.reveal(vscode.ViewColumn.Active); await refresh(); return; }
    previousEditor = vscode.window.activeTextEditor;
    panel = vscode.window.createWebviewPanel('waypoint.inspector', 'Paused values', vscode.ViewColumn.Active, { enableScripts: true, retainContextWhenHidden: true });
    const nonce = crypto.randomBytes(16).toString('hex');
    panel.webview.html = `<!doctype html><html lang="en"><head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}'">
      <style nonce="${nonce}">
        body{font:16px system-ui;color:var(--vscode-foreground);background:var(--vscode-editor-background);padding:12px}
        h1{font-size:24px}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}
        button{min-height:48px;font:inherit;border:1px solid var(--vscode-focusBorder);border-radius:6px;padding:10px;color:var(--vscode-button-secondaryForeground);background:var(--vscode-button-secondaryBackground)}
        button:focus-visible{outline:3px solid var(--vscode-focusBorder)}
        dt{font-weight:600;margin-top:16px;overflow-wrap:anywhere}dd{margin:6px 0;padding:12px;background:var(--vscode-textCodeBlock-background);white-space:pre-wrap;overflow-wrap:anywhere}p{line-height:1.5}
      </style></head><body><h1>Paused values</h1>
      <div class="actions"><button data-action="step">Step over</button><button data-action="continue">Continue</button><button data-action="refresh">Refresh values</button><button data-action="editor">Return to code</button></div>
      <p id="status" role="status">Reading debugger state…</p><dl id="values"></dl><p id="note"></p>
      <script nonce="${nonce}">
        const api=acquireVsCodeApi();
        document.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>api.postMessage(b.dataset.action)));
        window.addEventListener('message',({data})=>{
          document.getElementById('status').textContent=data.status;
          document.getElementById('note').textContent=data.note||'';
          const values=document.getElementById('values');values.replaceChildren();
          for(const v of data.variables){const name=document.createElement('dt'),value=document.createElement('dd');name.textContent=v.name;value.textContent=v.value;values.append(name,value)}
        });
        api.postMessage('refresh');
      </script></body></html>`;
    panel.webview.onDidReceiveMessage(async action => {
      if (action === 'refresh') { await refresh(); return; }
      if (action === 'editor') {
        panel.dispose();
        if (previousEditor) await vscode.window.showTextDocument(previousEditor.document, previousEditor.viewColumn);
        return;
      }
      const commands = { step: 'workbench.action.debug.stepOver', continue: 'workbench.action.debug.continue' };
      if (!Object.hasOwn(commands, action)) return;
      const command = commands[action];
      if (!command || !vscode.debug.activeDebugSession || !stopped.has(vscode.debug.activeDebugSession.id)) return;
      followStep = action === 'step';
      try { await vscode.commands.executeCommand(command); }
      catch (error) { vscode.window.showErrorMessage('Debugger action failed: ' + error.message); }
    }, undefined, context.subscriptions);
    panel.onDidDispose(() => { panel = undefined; revision++; });
  }));
}
exports.registerInspector = registerInspector;
