// SPDX-License-Identifier: Apache-2.0
'use strict';

(async () => {
  const config = await fetch('./waypoint-browser.json').then(response => {
    if (!response.ok) throw Error('Missing Waypoint candidate configuration');
    return response.json();
  });
  const narrow = matchMedia('(max-width: 640px)');
  const panel = document.createElement('nav');
  panel.id = 'wp-controls';
  panel.setAttribute('aria-label', 'Notebook actions');
  const status = document.createElement('p');
  status.id = 'wp-status';
  status.setAttribute('role', 'status');
  status.textContent = 'Loading notebook actions. Keep the hosted fallback available.';
  const announce = message => { status.textContent = message; };
  function button(parent, label, action) {
    const el = document.createElement('button');
    el.type = 'button';
    el.textContent = label;
    el.addEventListener('click', action);
    parent.append(el);
    return el;
  }
  const fallback = new URL(config.fallback);
  if (fallback.protocol !== 'https:' || fallback.username || fallback.password) {
    throw Error('Invalid hosted fallback');
  }
  const destination = document.createElement(fallback.hostname.endsWith('.invalid') ? 'span' : 'a');
  destination.textContent = 'Hosted fallback';
  if (destination.tagName === 'A') destination.href = fallback.origin;
  else destination.textContent += ' (ask your course operator)';
  let app;
  const main = () => document.getElementById('jp-main-dock-panel');
  function focusNotebook() {
    const target = main();
    if (!target) return;
    const cell = app.shell.currentWidget?.content?.activeCell?.node;
    if (cell && target.contains(cell)) { cell.focus(); return; }
    target.tabIndex = -1;
    target.focus();
  }
  const skip = button(panel, 'Go to notebook', focusNotebook);
  let filesRequested = false;
  const files = button(panel, 'Show files', () => {
    if (app.shell.leftCollapsed) {
      filesRequested = true;
      app.shell.expandLeft();
      if (narrow.matches) announce('Files shown. Choose Hide files to return to the full notebook.');
    } else {
      filesRequested = false;
      app.shell.collapseLeft();
      files.focus();
      announce('File panel hidden.');
    }
    sync();
  });
  const dialog = document.createElement('dialog');
  dialog.id = 'wp-export';
  dialog.setAttribute('aria-labelledby', 'wp-export-title');
  const heading = document.createElement('h2');
  heading.id = 'wp-export-title';
  heading.textContent = 'Download coursework files';
  const note = document.createElement('p');
  note.textContent = 'Download each file separately. Saving or downloading does not submit your work.';
  const feedback = document.createElement('p');
  feedback.setAttribute('role', 'status');
  dialog.append(heading, note, feedback);
  for (const name of config.files) {
    button(dialog, `Download ${name}`, async () => {
      try {
        await app.serviceManager.contents.get(name, {content: false});
        const url = new URL(await app.serviceManager.contents.getDownloadUrl(name), location.href);
        if (url.origin !== location.origin && url.protocol !== 'blob:') throw Error('Nonlocal download');
        const link = document.createElement('a');
        link.href = url.href;
        link.download = name;
        dialog.append(link);
        link.click();
        link.remove();
        feedback.textContent = `Download requested for ${name}. Check your downloads.`;
      } catch {
        feedback.textContent = `Could not download ${name}. Check that it exists in the course folder.`;
      }
    });
  }
  button(dialog, 'Close downloads', () => dialog.close());
  const exportButton = button(panel, 'Download files', () => {
    feedback.textContent = '';
    dialog.showModal();
  });
  dialog.addEventListener('close', () => exportButton.focus());
  const command = (label, id, done) => button(panel, label, async () => {
    if (!app.commands.isEnabled(id)) {
      announce('Open the course notebook and select its kernel first.');
      return;
    }
    announce(`${label} in progress.`);
    try { await app.commands.execute(id); announce(done); }
    catch { announce(`${label} did not finish. Check the notebook for errors.`); }
  });
  const run = command('Run all cells', 'notebook:run-all-cells', 'Run command finished. Check notebook outputs for errors.');
  const save = command('Save notebook', 'docmanager:save', 'Save command finished. Download a backup of your files.');
  panel.insertBefore(run, exportButton);
  panel.insertBefore(save, exportButton);
  panel.append(destination, status);
  document.body.prepend(panel);
  document.body.append(dialog);
  const actions = [skip, files, run, save, exportButton];
  actions.forEach(el => { el.disabled = true; });
  // Limit the compatibility layer to the pinned Lab view, not the hosted workspace.
  const deadline = performance.now() + 150000;
  while (!window.jupyterapp && performance.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  app = window.jupyterapp;
  if (!app || !app.shell.collapseLeft || !app.serviceManager?.contents) {
    announce('Notebook actions unavailable. Use the hosted fallback.');
    return;
  }
  await app.restored;
  const dock = main();
  if (!dock) return;
  if (document.getElementById('jp-left-stack')) files.setAttribute('aria-controls', 'jp-left-stack');
  let scheduled = false;
  const sizes = new ResizeObserver(() => queue());
  sizes.observe(dock);
  function sync() {
    const active = app.shell.currentWidget;
    const current = !!active?.node.classList.contains('jp-NotebookPanel') &&
      active.context?.path === config.notebook;
    if (narrow.matches && !filesRequested && !app.shell.leftCollapsed) app.shell.collapseLeft();
    files.setAttribute('aria-expanded', String(!app.shell.leftCollapsed));
    const fileLabel = app.shell.leftCollapsed ? 'Show files' : 'Hide files';
    if (files.textContent !== fileLabel) files.textContent = fileLabel;
    run.disabled = !current;
    save.disabled = !current;
    // Give virtualized notebook cells the article structure required by the feed.
    for (const feed of dock.querySelectorAll('.jp-Notebook .jp-WindowedPanel-viewport[role="feed"]')) {
      for (const cell of feed.querySelectorAll(':scope > .jp-Cell')) {
        if (!cell.hasAttribute('role')) cell.setAttribute('role', 'article');
        const n = Number(cell.dataset.windowedListIndex) + 1;
        const type = cell.classList.contains('jp-CodeCell') ? 'Code' : 'Text';
        const label = Number.isInteger(n) && n > 0 ? `${type} cell ${n}` : `${type} cell`;
        for (const editor of cell.querySelectorAll('.cm-content[role="textbox"]')) {
          if ((!editor.hasAttribute('aria-label') || editor.dataset.wpEditor) && !editor.hasAttribute('aria-labelledby')) {
            editor.dataset.wpEditor = 'true';
            if (editor.getAttribute('aria-label') !== `${label} editor`) editor.setAttribute('aria-label', `${label} editor`);
          }
        }
        for (const area of cell.querySelectorAll('.jp-RenderedMarkdown, .jp-RenderedHTML, .cm-scroller')) {
          const style = getComputedStyle(area);
          const scroll = ['auto','scroll'].includes(style.overflowX) && area.scrollWidth > area.clientWidth + 1 ||
            ['auto','scroll'].includes(style.overflowY) && area.scrollHeight > area.clientHeight + 1;
          if (scroll && (!area.hasAttribute('tabindex') || area.tabIndex < 0 && !area.matches('.cm-content'))) {
            area.dataset.wpOldTabindex = area.getAttribute('tabindex') ?? '';
            area.tabIndex = 0;
            area.classList.add('wp-scroll');
            area.setAttribute('role', 'region');
            area.setAttribute('aria-label', `${label} scroll area`);
          } else if (!scroll && area.classList.contains('wp-scroll')) {
            if (area.dataset.wpOldTabindex) area.setAttribute('tabindex', area.dataset.wpOldTabindex);
            else area.removeAttribute('tabindex');
            delete area.dataset.wpOldTabindex;
            area.removeAttribute('role');
            area.removeAttribute('aria-label');
            area.classList.remove('wp-scroll');
          }
        }
      }
    }
    for (const host of document.querySelectorAll('jp-button[title="Upload Files"], jp-button[data-command="filebrowser:toggle-file-filter"], jp-button[title="More commands"]')) {
      // The real button is in shadow DOM. Forward toggle state there only.
      const native = host.shadowRoot?.querySelector('button');
      if (!native) continue;
      host.removeAttribute('aria-pressed');
      if (host.dataset.command === 'filebrowser:toggle-file-filter') {
        const filter = host.closest('.jp-FileBrowser')?.querySelector('.jp-FileBrowser-filterBox');
        native.setAttribute('aria-pressed', String(!!filter?.getClientRects().length));
      }
    }
  }
  const queue = () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; sync(); });
  };
  const collapse = () => {
    filesRequested = false;
    if (narrow.matches) { app.shell.collapseLeft(); app.shell.collapseRight(); }
    queue();
  };
  narrow.addEventListener('change', collapse);
  app.shell.currentChanged.connect(queue);
  app.shell.layoutModified.connect(queue);
  new MutationObserver(queue).observe(app.shell.node, {subtree: true, childList: true, attributes: true,
    attributeFilter: ['class', 'role', 'aria-pressed', 'aria-selected']});
  new ResizeObserver(() => {
    document.body.style.setProperty('--wp-controls-height', `${panel.offsetHeight}px`);
    app.shell.update();
    queue();
  }).observe(panel);
  document.body.classList.add('wp-ready');
  actions.forEach(el => { el.disabled = false; });
  collapse();
  sync();
  announce('Browser rehearsal. Save and download a backup before leaving.');
})().catch(error => console.error('Waypoint browser candidate:', error.message));
