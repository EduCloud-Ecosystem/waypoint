// SPDX-License-Identifier: Apache-2.0
const vscode = require('vscode');
const crypto = require('crypto');
const layout = {
  'editor.wordWrap': 'on',
  'editor.fontSize': 16,
  'editor.minimap.enabled': false,
  'workbench.editor.showTabs': 'single',
  'workbench.activityBar.location': 'hidden',
  'workbench.panel.opensMaximized': 'always',
  'debug.focusEditorOnBreak': false,
  'terminal.integrated.gpuAcceleration': 'off',
  'terminal.integrated.tabs.enabled': false,
};
function activate(context) {
  require('./inspector').registerInspector(context);
  let controls;
  const actions = {
    files: ['Open file', 'workbench.action.quickOpen'],
    search: ['Search project', 'workbench.action.findInFiles'],
    terminal: ['Terminal', 'workbench.action.terminal.focus'],
    git: ['Source control', 'workbench.view.scm'],
    save: ['Save file', 'workbench.action.files.save'],
    tests: ['Run tests', 'workbench.action.tasks.test'],
    inspect: ['Inspect paused values', 'waypoint.inspect'],
    debug: ['Run and Debug', 'workbench.view.debug'],
    breakpoint: ['Toggle breakpoint', 'editor.debug.action.toggleBreakpoint'],
    start: ['Start debugging', 'workbench.action.debug.start'],
    step: ['Step over', 'workbench.action.debug.stepOver'],
    continue: ['Continue', 'workbench.action.debug.continue'],
    select: ['Select all', 'editor.action.selectAll'],
    copy: ['Copy', 'editor.action.clipboardCopyAction'],
    cut: ['Cut', 'editor.action.clipboardCutAction'],
    paste: ['Paste', 'editor.action.clipboardPasteAction'],
    outdent: ['Outdent line', 'editor.action.outdentLines'],
    indent: ['Indent line', 'editor.action.indentLines'],
    undo: ['Undo', 'undo'],
    redo: ['Redo', 'redo'],
    commands: ['Command palette', 'workbench.action.showCommands'],
  };
  async function setLayout(phone) {
    if (phone) {
      await vscode.commands.executeCommand('workbench.action.closeSidebar');
      await vscode.commands.executeCommand('workbench.action.closeAuxiliaryBar');
      await vscode.commands.executeCommand('workbench.action.closePanel');
    }
    for (const [key, value] of Object.entries(layout)) {
      const config = vscode.workspace.getConfiguration();
      const savedKey = 'previous.' + key;
      if (phone) {
        if (!context.workspaceState.get('phone')) {
          await context.workspaceState.update(savedKey, config.inspect(key)?.workspaceValue);
        }
        await config.update(key, value, vscode.ConfigurationTarget.Workspace);
      } else {
        await config.update(key, context.workspaceState.get(savedKey), vscode.ConfigurationTarget.Workspace);
      }
    }
    await context.workspaceState.update('phone', phone);
  }
  context.subscriptions.push(vscode.commands.registerCommand('waypoint.phone', () => setLayout(true)));
  context.subscriptions.push(vscode.commands.registerCommand('waypoint.desktop', async () => {
    if (context.workspaceState.get('phone')) await setLayout(false);
  }));
  context.subscriptions.push(vscode.commands.registerCommand('waypoint.mobile', () => {
    if (controls) { controls.reveal(vscode.ViewColumn.Active); return; }
    const previousEditor = vscode.window.activeTextEditor;
    controls = vscode.window.createWebviewPanel('waypoint.mobile', 'Workspace controls',
      vscode.ViewColumn.Active, { enableScripts: true, retainContextWhenHidden: true });
    const nonce = crypto.randomBytes(16).toString('hex');
    const buttons = Object.entries(actions).map(([id, [label]]) =>
      '<button data-action="' + id + '">' + label + '</button>').join('');
    controls.webview.html = '<!doctype html><html lang="en"><head>' +
      '<meta name="viewport" content="width=device-width, initial-scale=1">' +
      '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;nonce-' + nonce + '&#39;; script-src &#39;nonce-' + nonce + '&#39;">' +
      '<style nonce="' + nonce + '">' +
      'body{font:16px system-ui;color:var(--vscode-foreground);background:var(--vscode-editor-background);padding:16px}' +
      'h1{font-size:24px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;max-width:620px}' +
      'button{font:inherit;min-height:48px;text-align:left;padding:12px;border:1px solid var(--vscode-focusBorder);background:var(--vscode-button-secondaryBackground);color:var(--vscode-button-secondaryForeground);border-radius:6px}' +
      'button:focus-visible{outline:3px solid var(--vscode-focusBorder);outline-offset:2px}p{line-height:1.5}' +
      '</style></head><body><h1>Workspace controls</h1><p>The same project on your phone and laptop.</p><div class="grid">' +
      buttons + '<button data-action="phone">Use phone layout</button><button data-action="desktop">Restore previous layout</button></div>' +
      '<script nonce="' + nonce + '">const api=acquireVsCodeApi();document.querySelectorAll("button").forEach(b=>b.addEventListener("click",()=>api.postMessage(b.dataset.action)));</script></body></html>';
    controls.webview.onDidReceiveMessage(async action => {
      if (action === 'phone' || action === 'desktop') {
        await vscode.commands.executeCommand('waypoint.' + action);
        return;
      }
      if (!Object.hasOwn(actions, action)) return;
      controls.dispose();
      if (previousEditor) {
        await vscode.window.showTextDocument(previousEditor.document, previousEditor.viewColumn);
      }
      try {
        await vscode.commands.executeCommand(actions[action][1]);
      } catch (error) {
        vscode.window.showErrorMessage('Workspace action failed: ' + error.message);
      }
    }, undefined, context.subscriptions);
    controls.onDidDispose(() => { controls = undefined; });
  }));
  const entry = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 1000);
  entry.text = '$(device-mobile) Workspace';
  entry.tooltip = 'Open touch-friendly workspace controls';
  entry.command = 'waypoint.mobile';
  entry.show();
  context.subscriptions.push(entry);
}
exports.activate = activate;
