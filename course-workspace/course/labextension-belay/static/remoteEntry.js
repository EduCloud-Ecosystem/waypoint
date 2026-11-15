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
  function panelBridge(app, tracker, Widget, base, status, narrow) {
    let panel, frame, nonce, pending, loaded = false, busy = false;
    function send() {
      if (!loaded || !pending) return;
      const snapshot = pending; pending = null;
      frame.contentWindow.postMessage({type:'belay-notebook-snapshot', version:1, nonce,
        source:snapshot.source, dirty:snapshot.dirty}, location.origin);
    }
    const receive = event => {
      if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin) return;
      const v = event.data;
      if (!v || Array.isArray(v) || v.version !== 1 || v.nonce !== nonce) return;
      const keys = Object.keys(v).sort().join(',');
      if (ready(event, frame.contentWindow, location.origin, nonce)) {loaded = true; send();}
      else if (v.type === 'belay-notebook-state' && keys === 'busy,nonce,type,version' && typeof v.busy === 'boolean') busy = v.busy;
      else if (v.type === 'belay-notebook-captured' && keys === 'accepted,nonce,type,version' && typeof v.accepted === 'boolean')
        status.textContent = v.accepted ? 'Selected cell ready in the tutor panel. Review it before asking.' : 'A hint is still pending. Keep its current context until it finishes.';
      else if (v.type === 'belay-notebook-return' && keys === 'nonce,type,version' && tracker.currentWidget) {
        app.shell.collapseRight();
        app.shell.activateById(tracker.currentWidget.id);
      }
    };
    window.addEventListener('message', receive);
    function activatePanel() {
      if (narrow.matches) app.shell.collapseLeft();
      app.shell.activateById(panel.id);
      if (loaded) frame.contentDocument.getElementById('tutor-heading')?.focus();
      else frame.focus();
    }
    return snapshot => {
      if (busy) {status.textContent = 'A hint is still pending. Wait before selecting another cell.'; activatePanel(); return;}
      pending = snapshot;
      if (!panel) {
        nonce = Array.from(crypto.getRandomValues(new Uint8Array(32)), value => value.toString(16).padStart(2, '0')).join('');
        const node = document.createElement('div'); node.className = 'educloud-tutor-panel';
        frame = document.createElement('iframe'); frame.title = 'Course tutor';
        frame.src = base + '#belay-notebook=' + nonce;
        node.append(frame); panel = new Widget({node}); panel.id = 'waypoint-belay-panel';
        panel.title.label = 'Tutor'; panel.title.caption = 'Optional course tutoring';
        panel.title.closable = false;
        app.shell.add(panel, 'right', {rank:100});
      }
      activatePanel(); send();
    };
  }
  if (typeof module !== 'undefined') module.exports = {capture, ready, panelBridge};
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
          let integrated = false;
          try {
            const response = await fetch(base + 'config', {credentials:'same-origin', cache:'no-store', redirect:'error', referrerPolicy:'same-origin',
              headers:{'X-XSRFToken':decodeURIComponent(cookie.slice(6))}});
            if (!response.ok) return;
            const config = await response.json();
            if (config.version !== 1 || config.base + '/' !== base || config.max_source_bytes !== LIMIT) return;
            integrated = config.notebook_panel === true;
          } catch (_) {return;}
          const node = document.createElement('div');
          const button = document.createElement('button'); button.type = 'button';
          button.textContent = 'Ask Belay about this code';
          const status = document.createElement('span'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
          node.append(button, status);
          let showPanel;
          if (integrated) {
            document.body.dataset.educloudStudio = 'info511-nhanes-v1';
            const css = document.createElement('link'); css.rel = 'stylesheet'; css.href = base + 'studio.css'; document.head.append(css);
            const course = document.createElement('span'); course.className = 'educloud-course'; course.textContent = 'INFO 511 · NHANES';
            node.prepend(course); node.className = 'educloud-course-actions';
            button.textContent = 'Ask tutor'; button.setAttribute('aria-label', 'Ask Belay about this code');
            status.className = 'educloud-action-status';
            const narrow = window.matchMedia('(max-width: 600px)');
            const collapseFiles = () => { if (narrow.matches) app.shell.collapseLeft(); };
            narrow.addEventListener('change', collapseFiles);
            void app.restored.then(collapseFiles);
            showPanel = panelBridge(app, tracker, Widget, base, status, narrow);
          }
          let cancel = () => {};
          const command = 'waypoint-belay:ask-active-code';
          app.commands.addCommand(command, {label:'Ask Belay about this code', execute() {
            let snapshot;
            try {snapshot = capture(app, tracker);} catch (error) {
              if (integrated) status.className = 'educloud-action-error';
              status.textContent = error.message; return;
            }
            if (integrated) status.className = 'educloud-action-status';
            if (showPanel) {showPanel(snapshot); return;}
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
