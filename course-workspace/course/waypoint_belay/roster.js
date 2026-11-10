/* Explicit selected-code hints; reuse Belay request recovery, never persist content. */
(async function () {
  'use strict';
  const el = id => document.getElementById(id);
  const origin = location.origin, basePath = location.pathname.replace(/\/$/, '');
  const base = origin + basePath + '/api';
  window.BELAY_SESSION_PROXY = true; window.BELAY_AUTH_ORIGIN = origin;
  let profile, captured = false, optedOut = false, busy = false;
  const jobs = globalThis.belayAskJobs();
  function enabled() {el('ask').disabled = busy || optedOut || !captured || !profile?.available || profile.assistance !== 'hints' || Date.now() >= profile.expires_at * 1000;}
  function notice() {
    el('policy').textContent = !profile?.available || Date.now() >= profile.expires_at * 1000
      ? 'Help access expired or unavailable. Your saved work is unchanged; you can still submit it.'
      : profile.assistance === 'disabled' ? 'Your instructor has disabled AI assistance. Continue in your notebook and submit saved work normally.'
      : 'Hints are permitted. Only the code and question you review here will be sent to the course tutoring service.';
    enabled();
  }
  el('no-ai').onclick = () => {optedOut = true; el('status').textContent = 'AI help is off for this page. Return to your notebook or submit selected saved files.'; enabled();};
  try {
    const headers = await window.BelayAuth.headers(base);
    const response = await fetch(basePath + '/profile', {headers, credentials:'same-origin',cache:'no-store',redirect:'error'});
    if (!response.ok) throw Error('Course help configuration is unavailable. Your saved work is unchanged.');
    profile = await response.json();
    el('assignment').textContent = 'Assignment: ' + profile.title;
    el('notebook').href = profile.notebook; el('submission').href = profile.submission;
    notice(); setInterval(notice, 1000);
  } catch (_) {el('policy').textContent = 'Course help is unavailable. Return to your notebook to keep working or submit saved files.'; return;}
  const nonce = /^#belay-notebook=([0-9a-f]{64})$/.exec(location.hash)?.[1];
  if (nonce && window.opener && window.opener !== window) {
    window.addEventListener('message', event => {
      const v = event.data;
      if (busy || event.origin !== origin || event.source !== window.opener || !v ||
          Object.keys(v).sort().join(',') !== 'dirty,nonce,source,type,version' ||
          v.type !== 'belay-notebook-snapshot' || v.version !== 1 || v.nonce !== nonce ||
          typeof v.source !== 'string' || !v.source.trim() || new TextEncoder().encode(v.source).length > 8192 || typeof v.dirty !== 'boolean') return;
      el('source').value = v.source; captured = true;
      el('capture').textContent = v.dirty ? 'Unsaved code captured. Save in your notebook before submitting.' : 'Code captured for review. Nothing has been sent or submitted.';
      enabled();
    });
    window.opener.postMessage({type:'belay-notebook-ready', version:1, nonce}, origin);
  }
  el('ask').onclick = async () => {
    enabled(); if (el('ask').disabled) return;
    if (!el('question').value.trim()) {el('status').textContent = 'Add your prediction and question first.'; el('question').focus(); return;}
    busy = true; enabled(); el('status').textContent = 'Checking permitted help…';
    try {
      const turn = await jobs.ask(base, '/api/sol/jobs', {exercise_id:profile.exercise_id,
        event:'chat',mode:'study',stance:'peer',source:el('source').value,
        recent:[{who:'student',text:el('question').value}]}, state => {el('status').textContent = 'Help request: ' + state + '. Your saved work is unchanged.';});
      el('reply').textContent = turn.message || 'No feedback available.';
      jobs.acknowledge(turn); el('status').textContent = 'Review this hint yourself. It is not a grade or submission.';
    } catch (e) {el('status').textContent = e.message + ' Your saved work is unchanged. Use the notebook, submission, or human help links.';}
    finally {busy = false; enabled();}
  };
})();
