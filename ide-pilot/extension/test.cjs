// SPDX-License-Identifier: Apache-2.0
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

test('phone layout restores workspace values and actions return to the source editor', async () => {
  const commands = new Map(), state = new Map(), values = new Map([['editor.fontSize', 21]]);
  const calls = [];
  let receive, dispose, html;
  const editor = { document: { uri: 'synthetic.py' }, viewColumn: 1 };
  const panel = {
    webview: {
      set html(value) { html = value; },
      onDidReceiveMessage(callback) { receive = callback; },
    },
    onDidDispose(callback) { dispose = callback; },
    dispose() { dispose(); }, reveal() {},
  };
  const vscode = {
    ViewColumn: { Active: 1 }, ConfigurationTarget: { Workspace: 2 }, StatusBarAlignment: { Left: 1 },
    commands: {
      registerCommand(name, callback) { commands.set(name, callback); return {}; },
      async executeCommand(name) { calls.push(name); return commands.get(name)?.(); },
    },
    workspace: { getConfiguration: () => ({
      inspect: key => ({ workspaceValue: values.get(key) }),
      update: async (key, value) => { if (value === undefined) values.delete(key); else values.set(key, value); },
    }) },
    window: {
      activeTextEditor: editor,
      createWebviewPanel: () => panel,
      createStatusBarItem: () => ({ show() {} }),
      async showTextDocument(document) { assert.equal(document, editor.document); calls.push('restore-editor'); },
      showErrorMessage(message) { throw Error(message); },
    },
  };
  const sandbox = { exports: {}, require: name => name === 'vscode' ? vscode : name === './inspector' ? { registerInspector() {} } : require(name) };
  vm.runInNewContext(fs.readFileSync(__dirname + '/extension.js', 'utf8'), sandbox);
  sandbox.exports.activate({ subscriptions: [], workspaceState: {
    get: key => state.get(key), update: async (key, value) => state.set(key, value),
  } });
  await commands.get('waypoint.desktop')();
  assert.equal(values.get('editor.fontSize'), 21, 'restore before phone mode must do nothing');
  await commands.get('waypoint.phone')();
  await commands.get('waypoint.phone')();
  assert.equal(values.get('editor.fontSize'), 16);
  assert.ok(calls.includes('workbench.action.closeAuxiliaryBar'));
  await commands.get('waypoint.desktop')();
  assert.equal(values.get('editor.fontSize'), 21);
  assert.equal(values.has('editor.wordWrap'), false);
  commands.get('waypoint.mobile')();
  assert.match(html, /Content-Security-Policy/);
  assert.match(html, /min-height:48px/);
  assert.match(html, /data-action="paste"/);
  const count = calls.length;
  await receive('unrecognized-action');
  assert.equal(calls.length, count, 'unrecognized webview messages cannot invoke commands');
  await receive('save');
  assert.deepEqual(calls.slice(-2), ['restore-editor', 'workbench.action.files.save']);
});
