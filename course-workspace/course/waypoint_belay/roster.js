/* Explicit selected-code hints; reuse Belay request recovery, never persist content. */
(async function () {
  'use strict';
  const el = id => document.getElementById(id);
  const origin = location.origin, basePath = location.pathname.replace(/\/$/, '');
  const base = origin + basePath + '/api';
  window.BELAY_SESSION_PROXY = true; window.BELAY_AUTH_ORIGIN = origin;
  let profile, captured = false, optedOut = false, busy = false, checkpointRequired = false, checkpointReady = false;
  let notebookWindow = null, nonce = null;
  const tellNotebook = (type, fields = {}) => {
    if (notebookWindow) notebookWindow.postMessage({type, version:1, nonce, ...fields}, origin);
  };
  const jobs = globalThis.belayAskJobs();
  function enabled() {el('ask').disabled = busy || optedOut || (checkpointRequired && !checkpointReady) || !captured || !profile?.available || profile.assistance !== 'hints' || Date.now() >= profile.expires_at * 1000;}
  function notice() {
    const expired = profile?.expires_at > 0 && Date.now() >= profile.expires_at * 1000;
    const policy = expired
      ? 'Tutor access ended. You can still save and submit while your workspace and assignment are open.'
      : !profile?.available ? 'Tutor access is unavailable. You can still save and submit while your workspace and assignment are open.'
      : profile.assistance === 'disabled' ? 'Your instructor has disabled AI assistance. Continue in your notebook and submit saved work normally.'
      : 'Hints are permitted. Review the selected code and your question before sending them to the course tutor.';
    if (el('policy').textContent !== policy) el('policy').textContent = policy;
    const remaining = Math.max(0, Math.ceil((profile?.expires_at || 0) - Date.now() / 1000));
    el('time-remaining').textContent = profile?.available && profile.assistance === 'hints' && remaining
      ? `Tutor time remaining: ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}` : '';
    enabled();
  }
  el('no-ai').onclick = () => {optedOut = true; el('status').textContent = 'AI help is off for this page. Return to your notebook or submit selected saved files.'; enabled();};
  try {
    const headers = await window.BelayAuth.headers(base);
    const response = await fetch(basePath + '/profile', {headers, credentials:'same-origin',cache:'no-store',redirect:'error'});
    if (!response.ok) throw Error('Course help configuration is unavailable. Your saved work is unchanged.');
    profile = await response.json();
    checkpointRequired = profile.exercise_id === 'info511-nhanes-wrangling';
    el('assignment').textContent = 'Assignment: ' + profile.title;
    el('notebook').href = profile.notebook; el('submission').href = profile.submission;
    notice(); setInterval(notice, 1000);
  } catch (_) {el('policy').textContent = 'Course help is unavailable. Return to your notebook to keep working or submit saved files.'; return;}
  // Only the registered NHANES task needs this explicit presence checkpoint.
  // The authenticated server owns the policy, revision and grant checks.
  if (profile.exercise_id === 'info511-nhanes-wrangling') {
    el('checkpoint-panel').hidden = false;
    const path = base + '/api/classroom/' + encodeURIComponent(profile.exercise_id) + '/' + encodeURIComponent(profile.exercise_version);
    async function checkpointRequest(body) {
      const headers = await window.BelayAuth.headers(base);
      if (body) headers['Content-Type'] = 'application/json';
      const r = await fetch(path, {method:body ? 'POST' : 'GET', headers, credentials:'same-origin', cache:'no-store', redirect:'error', ...(body ? {body:JSON.stringify(body)} : {})});
      if (!r.ok) throw Error('Checkpoint could not be confirmed. Your notebook is unchanged. Retry the checkpoint or continue without AI.');
      return r.json();
    }
    el('checkpoint-status').textContent = 'Record your prediction checkpoint to enable a hint.';
    checkpointRequest().then(state => { checkpointReady = checkpointReady || Boolean(state.checkpoint); enabled(); }).catch(e => { if (!checkpointReady) el('checkpoint-status').textContent = e.message; });
    el('checkpoint').onclick = async () => {
      if (!el('schema-present').checked || !el('unit-present').checked) {
        el('checkpoint-status').textContent = 'Write both predictions in your notebook and confirm both boxes first.'; return;
      }
      el('checkpoint').disabled = true;
      try {
        const state = await checkpointRequest();
        const saved = await checkpointRequest({request_id:crypto.randomUUID().replaceAll('-', ''), expected_revision:state.revision,
          action:'checkpoint', schema_present:true, unit_present:true});
        if (!saved.checkpoint) throw Error('Checkpoint was not confirmed.');
        checkpointReady = true; enabled();
        el('checkpoint-status').textContent = 'Prediction checkpoint recorded. You can now request a hint.';
      } catch (e) {el('checkpoint-status').textContent = e.message;}
      finally {el('checkpoint').disabled = false;}
    };
  }
  nonce = /^#belay-notebook=([0-9a-f]{64})$/.exec(location.hash)?.[1];
  const embedded = profile.notebook_panel === true && window.parent !== window;
  notebookWindow = embedded ? window.parent : window.opener;
  if (embedded) {
    document.body.classList.add('embedded');
    el('notebook').onclick = event => {event.preventDefault(); tellNotebook('belay-notebook-return');};
    el('submission').target = '_blank'; el('submission').rel = 'noopener';
  }
  if (nonce && notebookWindow && notebookWindow !== window) {
    window.addEventListener('message', event => {
      const v = event.data;
      if (event.origin !== origin || event.source !== notebookWindow || !v ||
          Object.keys(v).sort().join(',') !== 'dirty,nonce,source,type,version' ||
          v.type !== 'belay-notebook-snapshot' || v.version !== 1 || v.nonce !== nonce ||
          typeof v.source !== 'string' || !v.source.trim() || new TextEncoder().encode(v.source).length > 8192 || typeof v.dirty !== 'boolean') return;
      if (busy) {tellNotebook('belay-notebook-captured', {accepted:false}); return;}
      el('source').value = v.source; captured = true;
      el('capture').textContent = v.dirty ? 'Unsaved code captured. Save in your notebook before submitting.' : 'Code captured for review. Nothing has been sent or submitted.';
      enabled();
      if (embedded && document.hasFocus() && document.activeElement === document.body) el('tutor-heading').focus();
      if (embedded) tellNotebook('belay-notebook-captured', {accepted:true});
    });
    tellNotebook('belay-notebook-ready');
  }
  el('ask').onclick = async () => {
    enabled(); if (el('ask').disabled) return;
    if (!el('question').value.trim()) {el('status').textContent = 'Add your prediction and question first.'; el('question').focus(); return;}
    busy = true; enabled(); el('status').textContent = 'Waiting for a hint…';
    if (embedded) tellNotebook('belay-notebook-state', {busy:true});
    el('capture').textContent = 'Code selected for this help request. Saving and submission are separate.';
    try {
      const turn = await jobs.ask(base, '/api/sol/jobs', {exercise_id:profile.exercise_id,
        event:'chat',mode:'study',stance:'peer',source:el('source').value,
        recent:[{who:'student',text:el('question').value}]});
      el('reply').textContent = turn.message || 'No feedback available.';
      jobs.acknowledge(turn); el('status').textContent = 'Hint ready. Review it before using it in your notebook.';
    } catch (e) {
      if (e.status === 401 || e.status === 403) {profile.available = false; notice();}
      el('status').textContent = 'Could not get a hint. Your saved work is unchanged. Return to your notebook or ask your instructor for help.';
    }
    finally {busy = false; enabled(); if (embedded) tellNotebook('belay-notebook-state', {busy:false});}
  };
})();
