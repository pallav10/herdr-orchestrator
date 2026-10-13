/* ============================================================
   Presentation Stage — a shareable, viewport-filling live view.
   It changes only HTML layout and URL state; SVG semantics and export
   serialization remain untouched. Escape first clears an active guided
   view/focus, then exits the stage.
   ============================================================ */
Archify.presentation = (function () {
  var html = document.documentElement;
  var btn = document.getElementById('btn-present');
  var icon = document.getElementById('present-icon');
  var label = document.getElementById('present-label');
  var previousScrollY = 0;

  function active() { return html.getAttribute('data-present') === 'true'; }

  function updateUrl(next) {
    try {
      var url = new URL(window.location.href);
      if (next) url.searchParams.set('present', '1');
      else url.searchParams.delete('present');
      history.replaceState(null, '', url.pathname + url.search + url.hash);
    } catch (_) {}
  }

  function render(next) {
    btn.setAttribute('aria-pressed', next ? 'true' : 'false');
    btn.setAttribute('aria-label', next ? 'Exit presentation stage' : 'Enter presentation stage');
    btn.title = next ? 'Exit presentation stage (F or Escape)' : 'Presentation stage (F)';
    icon.textContent = next ? '\u00d7' : '\u25a1';
    label.textContent = next ? 'Exit' : 'Present';
  }

  function setActive(next, options) {
    options = options || {};
    next = Boolean(next);
    if (next === active()) {
      render(next);
      return next;
    }
    if (next) {
      if (Archify.semanticLens && typeof Archify.semanticLens.clearPreview === 'function') Archify.semanticLens.clearPreview();
      previousScrollY = window.scrollY || 0;
      html.setAttribute('data-present', 'true');
      try { window.scrollTo(0, 0); } catch (_) {}
    } else {
      html.removeAttribute('data-present');
    }
    render(next);
    if (options.updateUrl !== false) updateUrl(next);
    requestAnimationFrame(function () {
      if (Archify.view && typeof Archify.view.reset === 'function') {
        Archify.view.reset({ automatic: true });
        requestAnimationFrame(function () {
          if (Archify.view && typeof Archify.view.sync === 'function') Archify.view.sync();
        });
      }
      if (!next && previousScrollY) {
        try { window.scrollTo(0, previousScrollY); } catch (_) {}
      }
    });
    return next;
  }

  function toggle() { return setActive(!active()); }

  render(active());
  btn.addEventListener('click', toggle);

  return {
    enter: function () { return setActive(true); },
    exit: function () { return setActive(false); },
    toggle: toggle,
    active: active
  };
})();
