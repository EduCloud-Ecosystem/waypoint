/* EduCloud Studio appearance v1. Stores only an enum, never course content. */
(() => {
  'use strict';
  const key = 'educloud-appearance-v1';
  const valid = value => ['light', 'dark', 'system'].includes(value);
  const media = matchMedia('(prefers-color-scheme: dark)');
  let preference = 'light';
  try { const stored = localStorage.getItem(key); if (valid(stored)) preference = stored; } catch (_) {}
  const supplied = new URL(location.href).searchParams.get('appearance');
  if (valid(supplied)) { preference = supplied; try { localStorage.setItem(key, preference); } catch (_) {} }
  function apply() {
    const resolved = preference === 'system' ? (media.matches ? 'dark' : 'light') : preference;
    document.documentElement.dataset.studioTheme = resolved;
    document.documentElement.dataset.studioPreference = preference;
    document.documentElement.style.colorScheme = resolved;
    document.querySelectorAll('[data-studio-theme-picker]').forEach(node => { node.value = preference; });
    window.dispatchEvent(new CustomEvent('educloud-appearance', {detail:{preference, resolved}}));
  }
  function set(value) {
    if (!valid(value)) return;
    preference = value;
    try { localStorage.setItem(key, value); } catch (_) {}
    apply();
  }
  window.EduCloudAppearance = Object.freeze({set, get:() => preference,
    resolved:() => preference === 'system' ? (media.matches ? 'dark' : 'light') : preference});
  document.addEventListener('change', event => {
    if (event.target.matches('[data-studio-theme-picker]')) set(event.target.value);
  });
  window.addEventListener('storage', event => { if (event.key === key) { preference = valid(event.newValue) ? event.newValue : 'light'; apply(); } });
  media.addEventListener('change', () => { if (preference === 'system') apply(); });
  document.addEventListener('DOMContentLoaded', apply);
  apply();
})();
