// Composes the selected diagram (?tab=<id>, default: first registered) before the Archify runtime binds to it.
(() => {
  const { components: c, diagrams } = HerdrVisual;
  const requested = new URLSearchParams(location.search).get('tab');
  const diagram = diagrams.find((d) => d.id === requested) || diagrams[0];
  document.title = `${diagram.title} Diagram`;
  document.body.insertAdjacentHTML('afterbegin', [
    c.toolbar(),
    '<div class="container">',
    c.tabNav(diagrams, diagram.id),
    c.header(diagram),
    c.guidedViews(diagram),
    c.diagramStage(diagram),
    c.cards(diagram),
    c.footer(diagram),
    '</div>'
  ].join('\n'));
})();
