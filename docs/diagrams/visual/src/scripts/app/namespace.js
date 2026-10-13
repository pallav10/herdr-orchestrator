// Shared registry for page components and diagram data. Loaded before every other app script.
window.HerdrVisual = {
  components: {},
  diagrams: [],
  escape(value) {
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    return String(value ?? '').replace(/[&<>"']/g, (c) => map[c]);
  }
};
