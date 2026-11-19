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
  function panelBridge(app, tracker, Widget, base, status, narrow, refitOnResize = false) {
    let panel, frame, nonce, pending, loaded = false, busy = false, explicitActivation = false;
    if (refitOnResize) narrow.addEventListener('change', () => {panel?.parent?.fit(); app.shell.fit();});
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
      explicitActivation = true;
      if (narrow.matches) app.shell.collapseLeft();
      app.shell.activateById(panel.id);
      if (refitOnResize) {panel.parent?.fit(); app.shell.fit();}
      if (loaded) frame.contentDocument.getElementById('tutor-heading')?.focus();
      else frame.focus();
    }
    function ensure(action, notice) {
      if (panel) return;
      nonce = Array.from(crypto.getRandomValues(new Uint8Array(32)), value => value.toString(16).padStart(2, '0')).join('');
      const node = document.createElement('div'); node.className = 'educloud-tutor-panel';
      if (action) {
        const controls = document.createElement('div'); controls.className = 'educloud-tutor-controls';
        controls.append(action, notice); node.append(controls);
      }
      frame = document.createElement('iframe'); frame.title = 'Course tutor';
      frame.src = base + '#belay-notebook=' + nonce;
      node.append(frame); panel = new Widget({node}); panel.id = 'waypoint-belay-panel';
      panel.title.label = 'Tutor'; panel.title.caption = 'Tutor: review your question and request a hint';
      panel.title.closable = false;
      if (action) panel.onActivateRequest = () => {
        if (narrow.matches) app.shell.collapseLeft();
        panel.parent?.fit(); app.shell.fit();
        if (explicitActivation) {
          if (loaded) frame.contentDocument.getElementById('tutor-heading')?.focus();
          else frame.focus();
        } else action.focus({preventScroll:true});
        explicitActivation = false;
      };
      app.shell.add(panel, 'right', {rank:100});
    }
    const show = snapshot => {
      if (busy) {status.textContent = 'A hint is still pending. Wait before selecting another cell.'; activatePanel(); return;}
      pending = snapshot;
      ensure();
      activatePanel(); send();
    };
    show.register = ensure;
    return show;
  }
  function visualGuide(app, tracker, controls, askButton, reopen) {
    const group = document.createElement('section'); group.className = 'educloud-guide';
    group.setAttribute('aria-label', 'Visual guidance');
    const title = document.createElement('strong'); title.textContent = 'Show me';
    const message = document.createElement('p'); message.setAttribute('role', 'status');
    message.textContent = 'Point to a control without using it.';
    group.append(title); controls.append(group);
    let cleanup = () => {}, generation = 0;
    function clear(text = '', restore = false) {
      generation++; cleanup(); cleanup = () => {};
      if (text) message.textContent = text;
      if (restore && tracker.currentWidget) app.shell.activateById(tracker.currentWidget.id);
    }
    function show(kind, label) {
      clear();
      const notebook = tracker.currentWidget, cell = notebook?.content?.activeCell;
      if (!notebook || notebook.isDisposed || !cell || app.shell.currentWidget !== notebook) {
        message.textContent = 'Open a notebook and select a cell first.'; return false;
      }
      const target = kind === 'cell' ? cell.node : kind === 'ask' ? (typeof askButton === 'function' ? askButton() : askButton) :
        notebook.toolbar?.node.querySelector('[data-jp-item-name="' + kind + '"]');
      app.shell.collapseRight(); app.shell.activateById(notebook.id);
      notebook.parent?.fit(); app.shell.fit();
      const ticket = generation;
      const deadline = performance.now() + 1000;
      function render() {
        if (ticket !== generation) return;
        if (tracker.currentWidget !== notebook || notebook.content.activeCell !== cell || app.shell.currentWidget !== notebook) {
          clear('Selection changed. Choose the cue again.'); return;
        }
        const r = target?.getBoundingClientRect();
        const x = r && Math.max(3, r.left), y = r && Math.max(3, r.top);
        const right = r && Math.min(innerWidth - 3, r.right), bottom = r && Math.min(innerHeight - 3, r.bottom);
        const visible = target?.isConnected && r.width > 0 && r.height > 0 && right > x && bottom > y &&
          target.contains(document.elementFromPoint((x + right) / 2, (y + bottom) / 2));
        // Native sidebar collapse and virtualized cell layout settle asynchronously.
        // Recheck the same local element for at most one second; never invent a target.
        if (!visible && performance.now() < deadline) {requestAnimationFrame(render); return;}
        const ring = document.createElement('div'); ring.id = 'waypoint-guide-ring'; ring.setAttribute('aria-hidden', 'true');
        const tip = document.createElement('section'); tip.id = 'waypoint-guide-tip';
        tip.setAttribute('role', 'region'); tip.setAttribute('aria-label', 'Visual cue');
        const text = document.createElement('p'); text.id = 'waypoint-guide-description'; text.setAttribute('role', 'status');
        const explanations = {cell:'This is your selected cell.', run:'Run executes the selected cell.',
          save:'Save writes your notebook. Submission is a separate action.', ask:'Open the Tutor side tab, then choose Review course example. A hint needs a separate explicit request.'};
        text.textContent = visible ? label + ': ' + explanations[kind] :
          label + ' is not currently visible. Widen the workspace or reveal the notebook toolbar, then try again.';
        message.textContent = text.textContent;
        const dismiss = document.createElement('button'); dismiss.type = 'button'; dismiss.textContent = 'Dismiss cue';
        dismiss.setAttribute('aria-describedby', text.id);
        dismiss.addEventListener('click', () => clear('', true));
        const again = document.createElement('button'); again.type = 'button'; again.textContent = 'Open guide';
        again.addEventListener('click', () => {clear(); reopen();});
        tip.append(text, dismiss, again); document.body.append(tip);
        if (visible) {
          ring.dataset.target = kind;
          ring.style.cssText = `left:${x}px;top:${y}px;width:${right-x}px;height:${bottom-y}px`;
          document.body.append(ring);
        }
        const cancel = () => clear('The view changed. Choose the cue again.', tip.contains(document.activeElement));
        const escape = event => {if (event.key === 'Escape') clear('', true);};
        window.addEventListener('resize', cancel); window.addEventListener('scroll', cancel, true);
        document.addEventListener('keydown', escape, true);
        const signals = [tracker.currentChanged, notebook.content.activeCellChanged, notebook.context?.model?.contentChanged];
        for (const signal of signals) signal?.connect(cancel);
        cleanup = () => {
          ring.remove(); tip.remove();
          window.removeEventListener('resize', cancel); window.removeEventListener('scroll', cancel, true);
          document.removeEventListener('keydown', escape, true);
          for (const signal of signals) signal?.disconnect(cancel);
        };
        // Let the native notebook finish restoring focus after the sidebar closes.
        requestAnimationFrame(() => {if (tip.isConnected) dismiss.focus({preventScroll:true});});
      }
      requestAnimationFrame(render);
    }
    for (const [kind, label] of [['cell','Selected cell'],['run','Run'],['save','Save'],['ask','Ask tutor']]) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
      button.addEventListener('click', event => {if (show(kind, label) === false) event.stopPropagation();}); group.append(button);
    }
    group.append(message);
    return clear;
  }
  const sideTab = id => document.querySelector('.jp-SideBar.jp-mod-right [data-id="' + id + '"]');
  function studioWelcome(app, tracker) {
    const key = 'educloud-studio-welcome-v1';
    let card, ring, previous, step = -1, seen = false;
    const steps = [
      ['Your notebook', 'Work in the center notebook. Run the first code cell to see both input tables. Then write your two predictions.', () => tracker.currentWidget?.node],
      ['Run and Save', 'Run executes the selected cell. Save writes your notebook to your workspace. Saving and submitting are separate actions.', () => tracker.currentWidget?.toolbar?.node],
      ['Your files', 'Use the file-browser tab on the left to open your notebook, data and saved results. Return to the notebook tab to keep working.', () => document.querySelector('.jp-SideBar.jp-mod-left [data-id="filebrowser"]')],
      ['Lecture notes and the guide', 'Use the Lecture notes tab on the right for the lesson. Its ☰ Guide menu holds the reading notes, NHANES diagram and Show me highlights.', () => sideTab('waypoint-lecture-panel')],
      ['Optional tutoring', 'When hints are enabled, open Tutor and choose Review course example. Confirm your predictions, then choose Ask for a hint. You can also finish without AI.', () => sideTab('waypoint-belay-panel')],
      ['Submit when ready', 'Save first, then choose Submit work at the top. Preview the files, confirm and keep your receipt. Appearance at the top switches light and dark themes.', () => document.querySelector('#waypoint-cairn-link')]
    ];
    function position() {
      if (!card) return;
      ring?.remove(); ring = null;
      const target = step < 0 ? document.querySelector('.jp-SideBar.jp-mod-right') : steps[step][2]();
      const r = target?.getBoundingClientRect();
      if (r && r.width > 0 && r.height > 0 && r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight) {
        ring = document.createElement('div'); ring.className = 'educloud-tour-ring'; ring.setAttribute('aria-hidden','true');
        ring.style.cssText = `left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px`;
        document.body.append(ring);
      }
    }
    function close(restore = true) {
      seen = true;
      try {localStorage.setItem(key, 'seen');} catch (_) { /* In-memory dismissal still works. */ }
      card?.remove(); ring?.remove(); card = ring = null;
      window.removeEventListener('resize', position); window.removeEventListener('scroll', position, true);
      document.removeEventListener('keydown', escape, true);
      if (restore) {
        if (previous?.isConnected && previous !== document.body && previous !== document.documentElement && previous.getClientRects().length) previous.focus({preventScroll:true});
        else if (tracker.currentWidget) app.shell.activateById(tracker.currentWidget.id);
      }
    }
    function escape(event) {if (event.key === 'Escape') {event.preventDefault(); event.stopPropagation(); close();}}
    function render(focus) {
      card?.remove(); ring?.remove();
      card = document.createElement('section'); card.id = 'waypoint-studio-welcome';
      card.setAttribute('role','region'); card.setAttribute('aria-label', step < 0 ? 'Welcome to Learning Studio' : 'Workspace tour');
      const heading = document.createElement('h2'); heading.id = 'waypoint-studio-welcome-title';
      heading.textContent = step < 0 ? 'Your side tabs' : `${step + 1} of ${steps.length} · ${steps[step][0]}`;
      const text = document.createElement('p'); text.id = 'waypoint-studio-welcome-description';
      text.textContent = step < 0 ? 'The tabs on the right open Tutor and Lecture notes. Select a tab again to tuck it away. In notes, ☰ Guide has highlights and the NHANES diagram.' : steps[step][1];
      card.setAttribute('aria-labelledby',heading.id); card.setAttribute('aria-describedby',text.id);
      // The first-use callout does not interrupt typing or take keyboard focus.
      if (step < 0) text.setAttribute('role','status');
      card.append(heading,text);
      const action = (label, callback) => {const b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.addEventListener('click',callback); card.append(b); return b;};
      if (step > 0) action('Previous', () => {step--; render(true);});
      const next = action(step < 0 ? 'Start quick tour' : step === steps.length - 1 ? 'Finish tour' : 'Next', () => {
        if (step === steps.length - 1) close(); else {step++; render(true);}
      });
      action(step < 0 ? 'Maybe later' : 'Skip tour', () => close());
      document.body.append(card); position();
      if (focus) next.focus({preventScroll:true});
    }
    function show(first = false) {
      previous = document.activeElement; step = first ? -1 : 0;
      render(!first);
      window.addEventListener('resize',position); window.addEventListener('scroll',position,true);
      document.addEventListener('keydown',escape,true);
    }
    void app.restored.then(() => {
      try {seen = localStorage.getItem(key) === 'seen';} catch (_) { /* No storage is required. */ }
      if (!seen && !card) show(true);
    });
    return () => show(true);
  }
  function lecturePanel(app, tracker, Widget, base, narrow, guidance = false, askButton = null, tour = null) {
    let panel, frame, back, toggle, menu, clearCue = () => {};
    narrow.addEventListener('change', () => {panel?.parent?.fit(); app.shell.fit();});
    const closeMenu = (focus = false) => {
      if (!menu) return;
      menu.hidden = true; toggle.setAttribute('aria-expanded','false');
      if (focus) toggle.focus({preventScroll:true});
    };
    function ensure() {
      if (panel) return;
      const node = document.createElement('div'); node.className = 'educloud-lecture-panel';
      back = document.createElement('button'); back.type = 'button'; back.textContent = 'Back to notebook';
      back.addEventListener('click', () => {
        closeMenu(); app.shell.collapseRight();
        if (tracker.currentWidget) app.shell.activateById(tracker.currentWidget.id);
        else sideTab('waypoint-lecture-panel')?.focus();
      });
      frame = document.createElement('iframe'); frame.title = 'INFO 511 lecture notes';
      // Native session authentication needs same-origin asset requests.
      // Scripts, forms, popups and top-level navigation remain sandboxed.
      frame.setAttribute('sandbox', 'allow-same-origin'); frame.src = base + 'lecture.html';
      let asset = 'lecture.html';
      const reload = document.createElement('button'); reload.type = 'button'; reload.textContent = 'Reload notes';
      reload.addEventListener('click', () => {frame.src = base + asset; closeMenu(true);});
      const controls = document.createElement('div'); controls.className = 'educloud-lecture-controls'; controls.append(back);
      const syncAppearance = () => {
        const theme = window.EduCloudAppearance?.resolved() === 'dark' ? 'dark' : 'light';
        frame.style.colorScheme = theme;
        if (frame.contentDocument) frame.contentDocument.documentElement.dataset.studioTheme = theme;
      };
      frame.addEventListener('load', syncAppearance);
      window.addEventListener('educloud-appearance', syncAppearance); syncAppearance();
      node.append(controls);
      if (guidance) {
        toggle = document.createElement('button'); toggle.type = 'button'; toggle.textContent = '☰ Guide';
        toggle.setAttribute('aria-expanded','false'); toggle.setAttribute('aria-controls','waypoint-lecture-guide');
        toggle.setAttribute('aria-label','Lecture guide'); controls.append(toggle);
        menu = document.createElement('div'); menu.id = 'waypoint-lecture-guide'; menu.className = 'educloud-lecture-menu'; menu.hidden = true;
        menu.setAttribute('role','region'); menu.setAttribute('aria-label','Lecture guide menu');
        toggle.addEventListener('click', () => {
          const opening = menu.hidden; menu.hidden = !opening; toggle.setAttribute('aria-expanded',String(opening));
          if (opening) menu.querySelector('button')?.focus({preventScroll:true});
        });
        menu.addEventListener('keydown', event => {if (event.key === 'Escape') {event.stopPropagation(); closeMenu(true);}});
        const views = document.createElement('div'); views.className = 'educloud-lecture-controls';
        for (const [name, file] of [['Reading notes','lecture.html'],['NHANES diagram','nhanes-diagram.html']]) {
          const view = document.createElement('button'); view.type = 'button'; view.textContent = name;
          view.addEventListener('click', () => {asset = file; frame.src = base + asset; closeMenu(true);}); views.append(view);
        }
        views.append(reload);
        if (tour) {
          const replay = document.createElement('button'); replay.type = 'button'; replay.textContent = 'Workspace tour';
          replay.addEventListener('click', () => {closeMenu(); clearCue(); app.shell.collapseRight(); if (tracker.currentWidget) app.shell.activateById(tracker.currentWidget.id); tour();}); views.append(replay);
        }
        menu.append(views); node.append(menu);
        clearCue = visualGuide(app, tracker, menu, askButton, () => {open(); toggle.click();});
        menu.addEventListener('click', event => {if (event.target.closest('.educloud-guide button')) closeMenu();});
        node.addEventListener('focusout', event => {if (event.relatedTarget && !controls.contains(event.relatedTarget) && !menu.contains(event.relatedTarget)) closeMenu();});
        frame.addEventListener('focus', () => closeMenu());
      } else controls.append(reload);
      node.append(frame); panel = new Widget({node}); panel.id = 'waypoint-lecture-panel';
      panel.title.label = 'Lecture notes'; panel.title.caption = 'Lecture notes: lesson, diagram and guide';
      panel.title.closable = false;
      panel.onActivateRequest = () => {
        if (narrow.matches) app.shell.collapseLeft();
        panel.parent?.fit(); app.shell.fit(); back.focus({preventScroll:true});
      };
      app.shell.add(panel, 'right', {rank:110});
    }
    function open() {
      ensure(); closeMenu();
      if (narrow.matches) app.shell.collapseLeft();
      app.shell.activateById(panel.id); panel.parent?.fit(); app.shell.fit(); back.focus({preventScroll:true});
    }
    open.register = ensure;
    return open;
  }
  if (typeof module !== 'undefined') module.exports = {capture, ready, panelBridge, lecturePanel, visualGuide, studioWelcome};
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
          let integrated = false, lecture = false, guidance = false, example;
          try {
            const response = await fetch(base + 'config', {credentials:'same-origin', cache:'no-store', redirect:'error', referrerPolicy:'same-origin',
              headers:{'X-XSRFToken':decodeURIComponent(cookie.slice(6))}});
            if (!response.ok) return;
            const config = await response.json();
            if (config.version !== 1 || config.base + '/' !== base || config.max_source_bytes !== LIMIT) return;
            integrated = config.notebook_panel === true;
            lecture = integrated && config.lecture_navigation === true;
            guidance = lecture && config.visual_guidance === true;
            if (guidance) {
              example = config.course_example_source;
              if (typeof example !== 'string' || !example.trim() || new TextEncoder().encode(example).length > LIMIT) return;
            }
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
            const palette = document.createElement('link'); palette.rel = 'stylesheet'; palette.href = base + 'studio-theme.css'; document.head.append(palette);
            const appearance = document.createElement('script'); appearance.src = base + 'studio-theme.js';
            const label = document.createElement('label'); label.className = 'studio-appearance'; label.append('Theme ');
            const picker = document.createElement('select'); picker.setAttribute('data-studio-theme-picker', ''); picker.setAttribute('aria-label', 'Appearance');
            for (const [value, text] of [['light','Light'],['dark','Dark'],['system','System']]) { const option = document.createElement('option'); option.value = value; option.textContent = text; picker.append(option); }
            label.append(picker); node.append(label);
            let themeQueue = Promise.resolve();
            const syncTheme = () => {
              const theme = window.EduCloudAppearance?.resolved() === 'dark' ? 'JupyterLab Dark' : 'JupyterLab Light';
              themeQueue = themeQueue.then(() => app.restored).then(() => {
                if (app.commands.hasCommand('apputils:change-theme')) return app.commands.execute('apputils:change-theme', {theme});
                throw Error('Notebook appearance command unavailable');
              }).catch(() => { status.className = 'educloud-action-error'; status.textContent = 'Notebook theme could not be changed. Use Settings → Theme, or reload the workspace.'; });
            };
            window.addEventListener('educloud-appearance', syncTheme);
            appearance.onload = () => { picker.value = window.EduCloudAppearance.get(); syncTheme(); };
            document.head.append(appearance);
            const course = document.createElement('span'); course.className = 'educloud-course'; course.textContent = 'INFO 511 · NHANES';
            node.prepend(course); node.className = 'educloud-course-actions';
            button.textContent = 'Ask tutor'; button.setAttribute('aria-label', 'Ask Belay about this code');
            status.className = 'educloud-action-status';
            const narrow = window.matchMedia('(max-width: 600px)');
            const collapseFiles = () => { if (narrow.matches) app.shell.collapseLeft(); };
            narrow.addEventListener('change', collapseFiles);
            void app.restored.then(collapseFiles);
            showPanel = panelBridge(app, tracker, Widget, base, status, narrow, lecture);
            if (guidance) {
              document.body.dataset.educloudNavigation = 'side-tabs';
              button.textContent = 'Review course example'; button.setAttribute('aria-label','Review course example');
              showPanel.register(button, status);
              const tour = studioWelcome(app, tracker);
              const notes = lecturePanel(app, tracker, Widget, base, narrow, true, () => sideTab('waypoint-belay-panel'), tour);
              notes.register();
              // Native tabs change selection on Enter but keep focus in the tab bar.
              // For these two student panels, move into their first usable control.
              document.addEventListener('keydown', event => {
                if (event.key !== 'Enter' && event.key !== ' ') return;
                const id = event.target.closest?.('.jp-SideBar.jp-mod-right [data-id]')?.dataset.id;
                if (id !== 'waypoint-belay-panel' && id !== 'waypoint-lecture-panel') return;
                event.preventDefault(); event.stopPropagation();
                if (id === 'waypoint-lecture-panel') notes();
                else app.shell.activateById(id);
              }, true);
            } else if (lecture) {
              const notes = document.createElement('button'); notes.type = 'button';
              notes.id = 'waypoint-lecture-open'; notes.textContent = 'Lecture notes';
              notes.addEventListener('click', lecturePanel(app, tracker, Widget, base, narrow));
              node.insertBefore(notes, status);
            }
          }
          let cancel = () => {};
          const command = 'waypoint-belay:ask-active-code';
          app.commands.addCommand(command, {label:guidance ? 'Review course example' : 'Ask Belay about this code', execute() {
            let snapshot;
            try {snapshot = guidance ? {source:example, dirty:false} : capture(app, tracker);} catch (error) {
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
