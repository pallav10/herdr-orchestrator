var Archify = {};

function hasDrawableGeometry(element) {
  if (!element) return false;
  var geometries = /^(path|line|polyline)$/i.test(element.tagName)
    ? [element]
    : Array.prototype.slice.call(element.querySelectorAll('path, line, polyline'));
  return geometries.some(function (geometry) {
    var source = geometry.tagName.toLowerCase() === 'path'
      ? geometry.getAttribute('d')
      : geometry.tagName.toLowerCase() === 'polyline'
        ? geometry.getAttribute('points')
        : [geometry.getAttribute('x1'), geometry.getAttribute('y1'), geometry.getAttribute('x2'), geometry.getAttribute('y2')].join(' ');
    if (!source || /(?:^|[^a-z])(?:nan|infinity)(?:[^a-z]|$)/i.test(source) || typeof geometry.getTotalLength !== 'function') return false;
    try {
      var length = Number(geometry.getTotalLength());
      return Number.isFinite(length) && length > 0;
    } catch (_) {
      return false;
    }
  });
}
