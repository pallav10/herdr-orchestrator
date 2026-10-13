// One link per registered diagram. Switching reloads the page so the single-diagram viewer starts clean.
HerdrVisual.components.tabNav = (diagrams, activeId) => {
  const e = HerdrVisual.escape;
  const current = new URLSearchParams(location.search);
  const href = (diagram) => {
    const params = new URLSearchParams({ tab: diagram.id });
    if (current.has('present')) params.set('present', current.get('present'));
    return `?${params}`;
  };
  const links = diagrams.map((diagram) => {
    const active = diagram.id === activeId ? ' aria-current="page"' : '';
    return `  <a class="diagram-tab" href="${e(href(diagram))}"${active}>${e(diagram.tab)}</a>`;
  });
  return `<nav class="diagram-tabs no-print" aria-label="Diagrams">\n${links.join('\n')}\n</nav>`;
};
