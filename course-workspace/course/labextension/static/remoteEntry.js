/* A dependency-free remote container for the pinned JupyterLab 4 image.
 * Uses the host's shared launcher token and Lumino Widget; ships no duplicate
 * framework code, network loader, user content, or build-time dependencies.
 */
(() => {
  'use strict';
  let shared;
  async function hostModule(name, major) {
    const versions = shared[name] || {};
    const version = Object.keys(versions).filter(value => value.startsWith(major + '.')).sort((a, b) => a.localeCompare(b, undefined, {numeric: true})).pop();
    if (!version) throw new Error('Unsupported JupyterLab host module');
    const provider = versions[version];
    provider.loaded = 1;
    return (await provider.get())();
  }
  window._JUPYTERLAB = window._JUPYTERLAB || {};
  window._JUPYTERLAB['@educloud/waypoint-cairn'] = {
    init(scope) { shared = scope; },
    async get(name) {
      if (name !== './extension') throw new Error('Unknown Waypoint extension');
      const [{ILauncher}, {Widget}] = await Promise.all([hostModule('@jupyterlab/launcher', 4), hostModule('@lumino/widgets', 2)]);
      return () => ({__esModule: true, default: {
        id: '@educloud/waypoint-cairn:plugin', autoStart: true, requires: [ILauncher],
        async activate(app, launcher) {
          const settings = JSON.parse(document.getElementById('jupyter-config-data').textContent);
          const base = settings.baseUrl.replace(/\/$/, '') + '/cairn/';
          let response;
          try { response = await fetch(base + 'config', {credentials: 'same-origin', cache: 'no-store', redirect: 'error'}); } catch (_) { return; }
          if (!response.ok) return;
          const command = 'waypoint-cairn:submit';
          app.commands.addCommand(command, {label: 'Submit saved files to Cairn', caption: 'Select saved coursework to review and submit in Cairn', execute: () => window.open(base, '_blank', 'noopener')});
          launcher.add({command, category: 'Coursework', rank: 0});
          const link = document.createElement('a'); link.href = base; link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Submit saved files to Cairn';
          link.style.cssText = 'display:flex;align-items:center;padding:0 12px;color:var(--jp-ui-font-color1);text-decoration:underline;font-size:var(--jp-ui-font-size1);white-space:nowrap';
          const widget = new Widget({node: link}); widget.id = 'waypoint-cairn-link';
          app.shell.add(widget, 'top', {rank: 900});
        }
      }});
    }
  };
})();
