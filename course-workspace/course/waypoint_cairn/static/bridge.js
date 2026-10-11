'use strict';
(async () => {
  const $ = id => document.getElementById(id);
  const selected = new Map();
  let config, directory = '', snapshot = null, popup = null, nonce = '', phase = 'idle', timer = null, deadline = 0, generation = 0;
  const status = text => { $('status').textContent = text; };
  const error = text => { $('error').textContent = text; };
  const stop = () => { clearInterval(timer); timer = null; nonce = ''; phase = 'idle'; $('send').disabled = false; };
  const invalidate = () => { generation++; stop(); snapshot = null; $('review').hidden = true; };
  const csrf = () => {
    const item = document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith('_xsrf='));
    if (!item) throw new Error('Session expired. Reload this page and sign in again.');
    return decodeURIComponent(item.slice(6));
  };
  async function request(path, options = {}) {
    const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(15000), ...options});
    if (!response.ok) throw new Error('Saved files unavailable. Check your session, supported filenames, and size limits, then retry.');
    return response.json();
  }
  function renderSelection() {
    $('selected').replaceChildren();
    for (const [path, size] of selected) {
      const row = document.createElement('li'); row.textContent = `${path} (${size} bytes) `;
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove';
      remove.setAttribute('aria-label', `Remove ${path}`);
      remove.onclick = () => { selected.delete(path); invalidate(); renderSelection(); browse(directory); };
      row.append(remove); $('selected').append(row);
    }
    $('prepare').disabled = !selected.size;
  }
  async function browse(path) {
    try {
      const data = await request(`${config.base}/list?directory=${encodeURIComponent(path)}`);
      directory = path; $('folder').textContent = directory || 'Workspace root'; $('up').disabled = !directory; $('files').replaceChildren();
      for (const entry of data.entries) {
        if (entry.directory) {
          const button = document.createElement('button'); button.type = 'button'; button.textContent = `Open folder: ${entry.name}`;
          button.onclick = () => browse(entry.path); $('files').append(button);
        } else {
          const label = document.createElement('label'), checkbox = document.createElement('input');
          checkbox.type = 'checkbox'; checkbox.checked = selected.has(entry.path); checkbox.disabled = entry.size > 1048576;
          checkbox.onchange = () => {
            error(''); invalidate();
            if (checkbox.checked && (selected.size >= 32 || [...selected.values()].reduce((a, b) => a + b, entry.size) > 4194304)) {
              checkbox.checked = false; error('Selection exceeds 32 files or 4 MiB total.');
            } else if (checkbox.checked) selected.set(entry.path, entry.size); else selected.delete(entry.path);
            renderSelection();
          };
          label.append(checkbox, document.createTextNode(`${entry.name} (${entry.size} bytes)${checkbox.disabled ? ' — exceeds 1 MiB' : ''}`)); $('files').append(label);
        }
      }
      status(data.entries.length ? 'Choose the saved files you want to send.' : 'This folder has no supported saved files.');
    } catch (e) { error(e.message); }
  }
  $('up').onclick = () => browse(directory.split('/').slice(0, -1).join('/'));
  $('cancel').onclick = () => { invalidate(); selected.clear(); renderSelection(); error(''); status('Selection cleared; nothing was submitted.'); browse(directory); };
  $('prepare').onclick = async () => {
    error(''); invalidate(); $('prepare').disabled = true;
    const preparedGeneration = generation;
    try {
      const data = await request(`${config.base}/files`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-XSRFToken': csrf()}, body: JSON.stringify({paths: [...selected.keys()]})});
      if (preparedGeneration !== generation) return;
      snapshot = data.files; $('snapshot').replaceChildren();
      for (const file of snapshot) {
        const li = document.createElement('li'); li.textContent = `${file.path} — ${file.size} bytes`;
        const hash = document.createElement('span'); hash.className = 'hash'; hash.textContent = `SHA-256: ${file.sha256}`; li.append(hash); $('snapshot').append(li);
      }
      $('review').hidden = false; status('Saved snapshot prepared. Continue in Cairn when ready.'); $('send').focus();
    } catch (e) { error(e.message); } finally { $('prepare').disabled = !selected.size; }
  };
  $('send').onclick = () => {
    if (!snapshot || phase !== 'idle') return;
    error(''); nonce = [...crypto.getRandomValues(new Uint8Array(32))].map(value => value.toString(16).padStart(2, '0')).join('');
    const fragment = encodeURIComponent(JSON.stringify({version: 1, origin: location.origin, nonce}));
    popup = window.open(`${config.origin}/me/submit#waypoint=${fragment}`, '_blank');
    if (!popup) { stop(); error('Allow popups for this workspace, then click Continue in Cairn again.'); return; }
    phase = 'waiting'; deadline = Date.now() + 10 * 60 * 1000; $('send').disabled = true;
    status('In Cairn, sign in, choose an assignment, and click Receive saved files. Nothing has been submitted.');
    timer = setInterval(() => {
      if (popup.closed || Date.now() > deadline) { stop(); error('Cairn was closed or the handoff expired. Click Continue in Cairn to retry.'); }
    }, 500);
  };
  window.addEventListener('message', event => {
    if (!config || !popup || phase === 'idle' || event.source !== popup || event.origin !== config.origin) return;
    const data = event.data;
    if (!data || data.version !== 1 || data.nonce !== nonce) return;
    if (data.type === 'cairn-ready' && phase === 'waiting') {
      phase = 'sent'; popup.postMessage({type: 'waypoint-files', version: 1, nonce, files: snapshot}, config.origin);
      status('Saved files sent; waiting for Cairn to validate them. Complete submission there.');
    } else if (data.type === 'cairn-received' && phase === 'sent') {
      stop(); snapshot = null; $('review').hidden = true; status('Cairn received the saved files for review. This is not a submission receipt; finish in Cairn.');
    }
  });
  window.addEventListener('pagehide', () => { stop(); snapshot = null; });
  try { config = await request('config'); await browse(''); } catch (e) { error(e.message); status('Cairn handoff is unavailable.'); }
})();
