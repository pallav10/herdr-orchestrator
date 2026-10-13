HerdrVisual.components.header = (diagram) => {
  const e = HerdrVisual.escape;
  const subtitle = diagram.subtitle ? `\n  <p class="subtitle">${e(diagram.subtitle)}</p>` : '';
  return `<div class="header">
  <div class="header-row">
    <div class="pulse-dot"></div>
    <h1>${e(diagram.title)}</h1>
  </div>${subtitle}
</div>`;
};
