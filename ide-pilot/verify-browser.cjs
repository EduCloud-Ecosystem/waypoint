// SPDX-License-Identifier: Apache-2.0
// Run against an isolated workspace created by prepare.py. Never a student project.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium, devices } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(process.argv[2]);
const mode = process.argv[3] || 'phone';
const evidence = path.join(root, 'state', 'verification');
fs.mkdirSync(evidence, { recursive: true });
const waitFor = async (check, label) => {
  for (let i = 0; i < 120; i++) {
    if (check()) return;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw Error('Timed out: ' + label);
};
(async () => {
  const browser = await chromium.launch({ channel: 'chrome' });
  const context = await browser.newContext(mode === 'phone' ? devices['Pixel 7'] : { viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const snapshot = async name => {
    await page.mouse.move(0, 400);
    await page.screenshot({ path: path.join(evidence, name + '.png'), scale: 'css' });
    fs.writeFileSync(path.join(evidence, name + '.txt'), await page.locator('body').ariaSnapshot());
  };
  const command = async name => {
    console.log('Command:', name);
    await page.keyboard.press('F1');
    await page.locator('.quick-input-widget input').first().fill('>' + name);
    await page.getByRole('option').filter({ hasText: name }).first().click();
    await page.waitForTimeout(1500);
  };
  const mobileAction = async name => {
    console.log('Touch action:', name);
    await page.getByRole('button', { name: /Open touch-friendly workspace controls/ }).tap();
    for (let i = 0; i < 100; i++) {
      for (const frame of page.frames()) {
        const button = frame.getByRole('button', { name, exact: true });
        if (await button.count()) { await button.tap(); return; }
      }
      await page.waitForTimeout(500);
    }
    throw Error('Missing mobile action: ' + name);
  };
  const terminal = async text => {
    const input = page.getByRole('textbox', { name: /Terminal \d+, bash/ }).first();
    await input.waitFor({ state: 'attached' });
    await input.focus();
    await page.keyboard.type(text);
    await page.keyboard.press('Enter');
  };
  try {
    await page.goto('http://127.0.0.1:58771/');
    await page.locator('input[type=password]').fill(fs.readFileSync(path.join(root, 'server.env'), 'utf8').trim().slice(9));
    await page.getByRole('button', { name: 'Submit' }).click();
    await page.getByRole('banner').getByRole('button', { name: 'Manage', exact: true }).click();
    await page.getByRole('button', { name: 'Trust', exact: true }).click();
    await page.getByRole('button', { name: /Close Modal Editor/ }).click();
    await page.getByRole('button', { name: /Open touch-friendly workspace controls/ }).waitFor({ timeout: 60000 });
    await command(mode === 'phone' ? 'Waypoint: Use phone layout' : 'Waypoint: Restore previous layout');
    if (mode === 'phone') await mobileAction('Open file');
    else await command('Go to File...');
    await page.locator('.quick-input-widget input').first().fill('analysis.py');
    await page.getByRole('option', { name: /^analysis.py/ }).first().click();
    if (mode === 'phone') {
      await mobileAction('Terminal');
      await terminal('python3 -m unittest -v > /home/coder/state/verification/before.txt 2>&1');
      await waitFor(() => fs.existsSync(path.join(evidence, 'before.txt')) && fs.readFileSync(path.join(evidence, 'before.txt'), 'utf8').includes('FAILED'), 'initial failing test');
      await page.getByRole('button', { name: /Hide Panel/ }).click();
      await page.getByRole('textbox', { name: 'analysis.py', exact: true }).focus();
      await command('Go to Line/Column...');
      await page.locator('.quick-input-widget input').first().fill(':6');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(700);
      await mobileAction('Toggle breakpoint');
      await mobileAction('Start debugging');
      await page.getByRole('button', { name: /^Continue \(/ }).waitFor({ timeout: 60000 });
      await mobileAction('Inspect paused values');
      let inspector;
      for (let i = 0; i < 100 && !inspector; i++) {
        for (const frame of page.frames()) {
          if (await frame.getByRole('button', { name: 'Refresh values', exact: true }).count()) { inspector = frame; break; }
        }
        if (!inspector) await page.waitForTimeout(500);
      }
      assert.ok(inspector, 'Focused inspector rendered');
      await inspector.getByText('[10, 20]', { exact: true }).waitFor();
      await inspector.getByText('[10, None, 20]', { exact: true }).waitFor();
      await snapshot('phone-debug');
      fs.writeFileSync(path.join(evidence, 'phone-debug-values.txt'), await inspector.locator('body').ariaSnapshot());
      await inspector.getByRole('button', { name: 'Step over', exact: true }).tap();
      await inspector.getByText('Paused: <module>, line 11', { exact: true }).waitFor();
      await inspector.getByRole('button', { name: 'Continue', exact: true }).tap();
      await inspector.getByText('Pause at a breakpoint to inspect values.', { exact: true }).waitFor();
      await page.getByRole('button', { name: /^Stop \(/ }).waitFor({ state: 'hidden' });
      await inspector.getByRole('button', { name: 'Return to code', exact: true }).tap();
      await page.getByRole('textbox', { name: 'analysis.py', exact: true }).waitFor();
      await page.waitForTimeout(500);
      await command('Waypoint: Use phone layout');
      const fixed = fs.readFileSync(path.join(__dirname, 'project', 'analysis.py'), 'utf8').replace('len(values)', 'len(valid)');
      await context.grantPermissions(['clipboard-read', 'clipboard-write']);
      await page.evaluate(text => navigator.clipboard.writeText(text), fixed);
      await page.getByRole('textbox', { name: 'analysis.py', exact: true }).focus();
      await mobileAction('Select all');
      await mobileAction('Paste');
      await mobileAction('Save file');
      await waitFor(() => fs.readFileSync(path.join(root, 'project', 'analysis.py'), 'utf8') === fixed, 'exact editor save');
      await snapshot('phone-editor');
      await mobileAction('Terminal');
      await terminal('python3 -m unittest -v > /home/coder/state/verification/after.txt 2>&1');
      await waitFor(() => fs.existsSync(path.join(evidence, 'after.txt')) && /\nOK\n/.test(fs.readFileSync(path.join(evidence, 'after.txt'), 'utf8')), 'passing tests');
      await terminal('git switch -c fix/missing-readings && git --no-pager diff -- analysis.py > /home/coder/state/verification/repair.diff');
      await waitFor(() => fs.existsSync(path.join(evidence, 'repair.diff')), 'Git diff');
      assert.match(fs.readFileSync(path.join(evidence, 'repair.diff'), 'utf8'), /\+    return sum\(valid\) \/ len\(valid\)/);
      await terminal('cat /home/coder/state/verification/repair.diff; git add analysis.py && git commit -m "Fix missing-reading denominator" && git rev-parse HEAD > /home/coder/state/verification/commit.txt');
      await waitFor(() => fs.existsSync(path.join(evidence, 'commit.txt')), 'Git commit');
      await snapshot('phone-git');
      await command('Waypoint: Use phone layout');
      await page.setViewportSize({ width: 320, height: 740 });
      await page.getByRole('button', { name: /Open touch-friendly workspace controls/ }).tap();
      for (let i = 0; i < 100; i++) {
        let frame;
        for (const candidate of page.frames()) {
          if (await candidate.getByRole('button', { name: 'Open file', exact: true }).count()) { frame = candidate; break; }
        }
        if (frame) {
          const boxes = await frame.getByRole('button').evaluateAll(buttons => buttons.map(b => { const r = b.getBoundingClientRect(); return { width: r.width, height: r.height, right: r.right }; }));
          assert.ok(boxes.every(b => b.width >= 44 && b.height >= 48 && b.right <= 320));
          await snapshot('phone-controls-320');
          break;
        }
        if (i === 99) throw Error('Narrow controls did not render');
        await page.waitForTimeout(500);
      }
    } else {
      await command('Terminal: Create New Terminal');
      await terminal('git branch --show-current > /home/coder/state/verification/laptop-branch.txt; git rev-parse HEAD > /home/coder/state/verification/laptop-commit.txt; python3 -m unittest -v > /home/coder/state/verification/laptop-tests.txt 2>&1; cat /home/coder/state/verification/laptop-tests.txt');
      await waitFor(() => fs.existsSync(path.join(evidence, 'laptop-tests.txt')) && /\nOK\n/.test(fs.readFileSync(path.join(evidence, 'laptop-tests.txt'), 'utf8')), 'laptop test run');
      assert.equal(fs.readFileSync(path.join(evidence, 'laptop-commit.txt'), 'utf8'), fs.readFileSync(path.join(evidence, 'commit.txt'), 'utf8'));
      assert.equal(fs.readFileSync(path.join(evidence, 'laptop-branch.txt'), 'utf8').trim(), 'fix/missing-readings');
      await snapshot('laptop-handoff');
    }
    console.log(mode.toUpperCase() + '_WORKFLOW_OK');
  } catch (error) {
    await snapshot(mode + '-failure');
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
