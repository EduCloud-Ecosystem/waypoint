// SPDX-License-Identifier: Apache-2.0
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('inspector reads the current stopped session and clears stale values on continue', async () => {
  let factory, receive, open, disposed, html;
  const messages = [], commands = [];
  const session = { id: 'synthetic', async customRequest(command, args) {
    if (command === 'stackTrace') { assert.equal(args.threadId, 7); return { stackFrames: [{ id: 10, name: 'mean', line: 6 }] }; }
    if (command === 'scopes') return { scopes: [{ name: 'Locals', expensive: false, variablesReference: 42 }] };
    assert.equal(command, 'variables'); assert.equal(args.variablesReference, 42);
    return { variables: [{ name: 'untrusted', value: '<script>alert(1)</script>' }] };
  } };
  const vscode = {
    ViewColumn: { Active: 1 },
    debug: {
      activeDebugSession: session,
      registerDebugAdapterTrackerFactory(type, value) { factory = value; return {}; },
      onDidTerminateDebugSession() { return {}; },
    },
    commands: {
      registerCommand(name, callback) { open = callback; return {}; },
      async executeCommand(name) { commands.push(name); },
    },
    window: {
      createWebviewPanel() { return {
        webview: {
          set html(value) { html = value; },
          async postMessage(value) { messages.push(value); },
          onDidReceiveMessage(callback) { receive = callback; },
        },
        onDidDispose(callback) { disposed = callback; }, dispose() { disposed(); },
      }; },
    },
  };
  const sandbox = { exports: {}, require: name => name === 'vscode' ? vscode : require(name) };
  vm.runInNewContext(fs.readFileSync(__dirname + '/inspector.js', 'utf8'), sandbox);
  sandbox.exports.registerInspector({ subscriptions: [] });
  const tracker = factory.createDebugAdapterTracker(session);
  await open();
  await receive('refresh');
  assert.equal(messages.at(-1).variables.length, 0);
  tracker.onDidSendMessage({ type: 'event', event: 'stopped', body: { threadId: 7 } });
  await receive('refresh');
  assert.equal(messages.at(-1).status, 'Paused: mean, line 6');
  assert.equal(messages.at(-1).variables[0].value, '<script>alert(1)</script>');
  assert.match(html, /value.textContent=v.value/);
  const count = commands.length;
  await receive('constructor');
  assert.equal(commands.length, count);
  await receive('step');
  assert.equal(commands.at(-1), 'workbench.action.debug.stepOver');
  tracker.onDidSendMessage({ type: 'event', event: 'continued' });
  await receive('refresh');
  assert.equal(messages.at(-1).variables.length, 0);
  await receive('step');
  assert.equal(commands.length, count + 1, 'stepping while running is ignored');
});
