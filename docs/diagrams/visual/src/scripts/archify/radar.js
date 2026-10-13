/* ============================================================
   Semantic Radar — simplified semantic bounds + live viewport.
   The radar is built at runtime so the checked artifact still contains
   one canonical SVG block, and export serialization remains untouched.
   ============================================================ */
Archify.radar = (function () {
  var container = document.querySelector('.diagram-container');
  var diagram = container.querySelector(':scope > svg');
  var panel = document.getElementById('overview-map');
  var surface = document.getElementById('overview-map-surface');
  var status = document.getElementById('overview-map-status');
  var trigger = document.getElementById('btn-overview-map');
  var closeBtn = document.getElementById('overview-map-close');
  var namespace = 'http://www.w3.org/2000/svg';
  var viewBox = diagram.viewBox && diagram.viewBox.baseVal;
  var mapSvg = document.createElementNS(namespace, 'svg');
  var nodeLayer = document.createElementNS(namespace, 'g');
  var viewport = document.createElementNS(namespace, 'rect');
  var nodes = [];
  var drag = null;
  var syncFrame = 0;

  mapSvg.setAttribute('role', 'group');
  mapSvg.setAttribute('aria-label', 'Semantic diagram radar nodes');
  mapSvg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  nodeLayer.setAttribute('class', 'overview-map-nodes');
  viewport.setAttribute('class', 'overview-map-viewport');
  viewport.setAttribute('rx', '3');
  viewport.setAttribute('ry', '3');
  mapSvg.appendChild(nodeLayer);
  mapSvg.appendChild(viewport);
  surface.appendChild(mapSvg);

  function nodeLabel(node, fallback) {
    return node.getAttribute('data-node-label') ||
      (node.getAttribute('aria-label') || fallback).replace(/^Focus\s+/, '').split(',')[0];
  }
  function build() {
    if (!viewBox || viewBox.width <= 0 || viewBox.height <= 0) return false;
    mapSvg.setAttribute('viewBox', [viewBox.x, viewBox.y, viewBox.width, viewBox.height].join(' '));
    nodeLayer.textContent = '';
    nodes = [];
    Array.prototype.forEach.call(diagram.querySelectorAll('[data-node-id]'), function (node) {
      var box;
      try { box = node.getBBox(); } catch (_) { box = null; }
      if (!box || box.width <= 0 || box.height <= 0) return;
      var id = node.getAttribute('data-node-id');
      var rect = document.createElementNS(namespace, 'rect');
      rect.setAttribute('class', 'overview-map-node');
      rect.setAttribute('x', String(box.x));
      rect.setAttribute('y', String(box.y));
      rect.setAttribute('width', String(Math.max(3, box.width)));
      rect.setAttribute('height', String(Math.max(3, box.height)));
      rect.setAttribute('rx', String(Math.max(2, Math.min(8, Math.min(box.width, box.height) * 0.1))));
      rect.setAttribute('data-radar-node-id', id);
      rect.setAttribute('data-kind', node.getAttribute('data-node-kind') || 'neutral');
      rect.setAttribute('tabindex', '0');
      rect.setAttribute('role', 'button');
      rect.setAttribute('aria-label', 'Focus ' + nodeLabel(node, id) + ' from Semantic Radar');
      nodeLayer.appendChild(rect);
      nodes.push({ id: id, node: node, rect: rect });
    });
    status.textContent = nodes.length + ' nodes · full map';
    return true;
  }
  function updateDocking() {
    if (panel.hidden) return;
    var containerRect = container.getBoundingClientRect();
    var intersectsWindow = containerRect.bottom > 12 && containerRect.top < window.innerHeight - 12;
    if (!intersectsWindow) {
      panel.removeAttribute('data-docked');
      panel.removeAttribute('data-dock-side');
      panel.style.removeProperty('--archify-radar-right');
      panel.style.removeProperty('--archify-radar-left');
      panel.style.removeProperty('--archify-radar-top');
      return;
    }
    var active = diagram.querySelector('[data-focus-selected]');
    var activeRect = active ? active.getBoundingClientRect() : null;
    var dockLeft = !!(activeRect && activeRect.left + activeRect.width / 2 > window.innerWidth / 2);
    var right = Math.max(16, window.innerWidth - Math.min(containerRect.right - 16, window.innerWidth - 16));
    var left = Math.max(16, Math.min(containerRect.left + 16, window.innerWidth - panel.offsetWidth - 16));
    var topLimit = Math.max(16, containerRect.top + 16);
    var bottomLimit = Math.min(window.innerHeight - 16, containerRect.bottom - 16);
    var panelLeft = dockLeft ? left : window.innerWidth - right - panel.offsetWidth;
    var panelRight = panelLeft + panel.offsetWidth;
    var lens = document.getElementById('focus-chip');
    var lensRect = lens && !lens.hidden ? lens.getBoundingClientRect() : null;
    var blockers = [activeRect, lensRect].filter(function (rect) {
      return rect && rect.right > panelLeft && rect.left < panelRight;
    });
    var desiredTop = bottomLimit - panel.offsetHeight;
    var candidates = [desiredTop, topLimit];
    blockers.forEach(function (rect) {
      candidates.push(rect.top - 10 - panel.offsetHeight, rect.bottom + 10);
    });
    var valid = candidates.filter(function (candidate) {
      if (candidate < topLimit || candidate + panel.offsetHeight > bottomLimit) return false;
      return blockers.every(function (rect) {
        return candidate + panel.offsetHeight <= rect.top - 10 || candidate >= rect.bottom + 10;
      });
    });
    valid.sort(function (a, b) { return Math.abs(a - desiredTop) - Math.abs(b - desiredTop); });
    var top = valid.length ? valid[0] : Math.max(topLimit, desiredTop);
    top = Math.min(window.innerHeight - panel.offsetHeight - 16, top);
    panel.setAttribute('data-docked', 'true');
    panel.setAttribute('data-dock-side', dockLeft ? 'left' : 'right');
    panel.style.setProperty('--archify-radar-right', Math.round(right) + 'px');
    panel.style.setProperty('--archify-radar-left', Math.round(left) + 'px');
    panel.style.setProperty('--archify-radar-top', Math.round(Math.max(16, top)) + 'px');
  }
  function syncNow() {
    syncFrame = 0;
    if (panel.hidden || !Archify.view || typeof Archify.view.logicalViewport !== 'function') return;
    updateDocking();
    var visible = Archify.view.logicalViewport();
    if (!visible) return;
    viewport.setAttribute('x', String(visible.x));
    viewport.setAttribute('y', String(visible.y));
    viewport.setAttribute('width', String(visible.width));
    viewport.setAttribute('height', String(visible.height));
    var full = visible.width >= viewBox.width * 0.98 && visible.height >= viewBox.height * 0.98;
    var mobileWide = window.innerWidth <= 720 && container.hasAttribute('data-wide-diagram');
    var viewportCopy = full
      ? 'full map'
      : (mobileWide ? Math.round(visible.width / viewBox.width * 100) + '% width' : Math.round(visible.scale * 100) + '% viewport');
    status.textContent = nodes.length + ' nodes · ' + viewportCopy;
    nodes.forEach(function (item) {
      var active = item.node.hasAttribute('data-focus-selected') ||
        item.node.getAttribute('data-story-beat-state') === 'active';
      if (active) item.rect.setAttribute('data-radar-active', 'true');
      else item.rect.removeAttribute('data-radar-active');
    });
  }
  function sync() {
    if (syncFrame) return;
    syncFrame = requestAnimationFrame(syncNow);
  }
  function setOpen(next, options) {
    options = options || {};
    next = Boolean(next);
    if (next && Archify.semanticLens && typeof Archify.semanticLens.clearPreview === 'function') Archify.semanticLens.clearPreview();
    if (next && Archify.semanticLens && Archify.semanticLens.isOpen()) {
      Archify.semanticLens.close({ restoreFocus: false });
    }
    panel.hidden = !next;
    trigger.setAttribute('aria-expanded', next ? 'true' : 'false');
    trigger.setAttribute('aria-label', next ? 'Close semantic radar' : 'Open semantic radar');
    if (next) {
      build();
      updateDocking();
      sync();
      if (options.focus === true) surface.focus();
    } else {
      panel.removeAttribute('data-docked');
      panel.removeAttribute('data-dock-side');
      panel.style.removeProperty('--archify-radar-right');
      panel.style.removeProperty('--archify-radar-left');
      panel.style.removeProperty('--archify-radar-top');
      if (options.restoreFocus === true) trigger.focus();
    }
    return next;
  }
  function toggle() { return setOpen(panel.hidden, { focus: false }); }
  function close(options) { return setOpen(false, options); }
  function bringNodeIntoWindow(node) {
    var delay = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 540;
    window.setTimeout(function () {
      var rect = node.getBoundingClientRect();
      var safeTop = 64;
      var safeBottom = window.innerHeight - 64;
      if (rect.top >= safeTop && rect.bottom <= safeBottom) return;
      var top = Math.max(0, window.scrollY + rect.top + rect.height / 2 - window.innerHeight / 2);
      try { window.scrollTo({ top: top, behavior: delay ? 'smooth' : 'auto' }); }
      catch (_) { window.scrollTo(0, top); }
    }, delay);
  }
  function focusNode(id) {
    var main = diagram.querySelector('[data-node-id="' + id + '"]');
    if (!main) return false;
    if (Archify.guidedViews && typeof Archify.guidedViews.showAll === 'function') {
      Archify.guidedViews.showAll({ clearFocus: false, updateUrl: false });
    }
    if (Archify.focus && typeof Archify.focus.set === 'function') {
      Archify.focus.set(id, { toggle: false });
    }
    if (Archify.view && typeof Archify.view.reveal === 'function') {
      Archify.view.reveal([id], { includeNeighbors: true, reason: 'radar' });
    }
    bringNodeIntoWindow(main);
    try { main.focus({ preventScroll: true }); } catch (_) { try { main.focus(); } catch (_) {} }
    sync();
    return true;
  }
  function diagramPoint(event) {
    var matrix = mapSvg.getScreenCTM();
    if (!matrix) return null;
    var point = mapSvg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    return point.matrixTransform(matrix.inverse());
  }
  function navigate(event) {
    var point = diagramPoint(event);
    if (!point || !Archify.view || typeof Archify.view.centerAt !== 'function') return;
    Archify.view.centerAt(point.x, point.y, { minimumScale: 1.5, instant: true });
    sync();
  }
  function endDrag(event) {
    if (!drag) return;
    drag = null;
    panel.removeAttribute('data-dragging');
    container.classList.remove('is-panning');
    try { surface.releasePointerCapture(event.pointerId); } catch (_) {}
  }

  trigger.addEventListener('click', toggle);
  closeBtn.addEventListener('click', function () { close({ restoreFocus: true }); });
  surface.addEventListener('pointerdown', function (event) {
    if (event.button !== 0 || event.target.closest('[data-radar-node-id]')) return;
    event.preventDefault();
    drag = { pointerId: event.pointerId };
    panel.setAttribute('data-dragging', 'true');
    container.classList.add('is-panning');
    try { surface.setPointerCapture(event.pointerId); } catch (_) {}
    navigate(event);
  });
  surface.addEventListener('pointermove', function (event) {
    if (!drag || drag.pointerId !== event.pointerId) return;
    navigate(event);
  });
  surface.addEventListener('pointerup', endDrag);
  surface.addEventListener('pointercancel', endDrag);
  surface.addEventListener('click', function (event) {
    var node = event.target.closest('[data-radar-node-id]');
    if (node) focusNode(node.getAttribute('data-radar-node-id'));
  });
  surface.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close({ restoreFocus: true });
      return;
    }
    var node = event.target.closest('[data-radar-node-id]');
    if (node && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      focusNode(node.getAttribute('data-radar-node-id'));
      return;
    }
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight' && event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    var visible = Archify.view && Archify.view.logicalViewport ? Archify.view.logicalViewport() : null;
    if (!visible) return;
    event.preventDefault();
    var x = visible.x + visible.width / 2;
    var y = visible.y + visible.height / 2;
    var stepX = Math.max(12, visible.width * 0.24);
    var stepY = Math.max(12, visible.height * 0.24);
    if (event.key === 'ArrowLeft') x -= stepX;
    else if (event.key === 'ArrowRight') x += stepX;
    else if (event.key === 'ArrowUp') y -= stepY;
    else y += stepY;
    Archify.view.centerAt(x, y, { minimumScale: 1.5, instant: true });
    sync();
  });
  window.addEventListener('resize', sync);
  window.addEventListener('scroll', function () { updateDocking(); sync(); }, { passive: true });
  requestAnimationFrame(build);

  return {
    open: function () { return setOpen(true); },
    close: close,
    toggle: toggle,
    sync: sync,
    focus: focusNode,
    isOpen: function () { return !panel.hidden; },
    count: function () { return nodes.length; }
  };
})();
