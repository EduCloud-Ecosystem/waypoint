/* Synthetic opt-in only. Uses JupyterLab's own tracker and Lumino modules.
 * Never serializes a notebook, Markdown, outputs, metadata, paths or identity.
 */
(() => {
  'use strict';
  const LIMIT = 8192;
  function capture(app, tracker) {
    const panel = tracker.currentWidget;
    const cell = panel?.content?.activeCell;
    if (!panel || app.shell.currentWidget !== panel || cell?.model?.type !== 'code')
      throw new Error('Select an active code cell in your notebook first.');
    const source = cell.model.sharedModel.getSource();
    const dirty = panel.context?.model?.dirty;
    if (typeof source !== 'string' || !source.trim() || new TextEncoder().encode(source).length > LIMIT || typeof dirty !== 'boolean')
      throw new Error('Select a nonempty code cell of at most 8192 UTF-8 bytes.');
    return Object.freeze({source, dirty});
  }
  function ready(event, popup, origin, nonce) {
    const data = event.data;
    return event.origin === origin && event.source === popup && data &&
      typeof data === 'object' && !Array.isArray(data) &&
      Object.keys(data).sort().join(',') === 'nonce,type,version' &&
      data.type === 'belay-notebook-ready' && data.version === 1 && data.nonce === nonce;
  }
  if (typeof module !== 'undefined') module.exports = {capture, ready};
  let shared;
  async function hostModule(name, major) {
    const versions = shared[name] || {};
    const version = Object.keys(versions).filter(value => value.startsWith(major + '.')).sort((a, b) => a.localeCompare(b, undefined, {numeric:true})).pop();
    if (!version) throw new Error('Unsupported JupyterLab host module');
    const provider = versions[version]; provider.loaded = 1;
    return (await provider.get())();
  }
  window._JUPYTERLAB = window._JUPYTERLAB || {};
  window._JUPYTERLAB['@educloud/waypoint-belay'] = {
    init(scope) {shared = scope;},
    async get(name) {
      if (name !== './extension') throw new Error('Unknown Waypoint extension');
      const [{INotebookTracker}, {Widget}] = await Promise.all([
        hostModule('@jupyterlab/notebook', 4), hostModule('@lumino/widgets', 2)]);
      return () => ({__esModule:true, default:{
        id:'@educloud/waypoint-belay:plugin', autoStart:true, requires:[INotebookTracker],
        async activate(app, tracker) {
          const settings = JSON.parse(document.getElementById('jupyter-config-data').textContent);
          const base = settings.baseUrl.replace(/\/$/, '') + '/belay/';
          const cookie = document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith('_xsrf='));
          if (!cookie) return;
          try {
            const response = await fetch(base + 'config', {credentials:'same-origin', cache:'no-store', redirect:'error', referrerPolicy:'same-origin',
              headers:{'X-XSRFToken':decodeURIComponent(cookie.slice(6))}});
            if (!response.ok) return;
            const config = await response.json();
            if (config.version !== 1 || config.base + '/' !== base || config.max_source_bytes !== LIMIT) return;
          } catch (_) {return;}
          const node = document.createElement('div');
          const button = document.createElement('button'); button.type = 'button';
          button.textContent = 'Ask Belay about this code';
          const status = document.createElement('span'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
          node.append(button, status);
          let cancel = () => {};
          const command = 'waypoint-belay:ask-active-code';
          app.commands.addCommand(command, {label:'Ask Belay about this code', execute() {
            let snapshot;
            try {snapshot = capture(app, tracker);} catch (error) {status.textContent = error.message; return;}
            cancel();
            const nonce = Array.from(crypto.getRandomValues(new Uint8Array(32)), value => value.toString(16).padStart(2, '0')).join('');
            const popup = window.open(base + '#belay-notebook=' + nonce, '_blank');
            if (!popup) {status.textContent = 'Allow the Belay popup, then try again.'; return;}
            let timer;
            const cleanup = () => {window.removeEventListener('message', receive); clearTimeout(timer);};
            const receive = event => {
              if (!ready(event, popup, location.origin, nonce)) return;
              cleanup();
              popup.postMessage({type:'belay-notebook-snapshot', version:1, nonce,
                source:snapshot.source, dirty:snapshot.dirty}, location.origin);
              status.textContent = 'Code snapshot sent for your review. Nothing was asked, saved or submitted.';
            };
            window.addEventListener('message', receive);
            timer = setTimeout(() => {cleanup(); status.textContent = 'Belay handoff expired. Choose the code action again.';}, 30000);
            cancel = cleanup;
            status.textContent = 'Opening Belay with this code snapshot. Review it before explicitly asking.';
          }});
          button.addEventListener('click', () => app.commands.execute(command));
          node.style.cssText = 'display:flex;align-items:center;gap:8px;padding:0 12px;font-size:var(--jp-ui-font-size1)';
          const widget = new Widget({node}); widget.id = 'waypoint-belay-link';
          app.shell.add(widget, 'top', {rank:901});
        }
      }});
    }
  };
})();
