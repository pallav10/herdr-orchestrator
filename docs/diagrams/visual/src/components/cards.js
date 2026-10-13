HerdrVisual.components.cards = (diagram) => {
  const e = HerdrVisual.escape;
  const card = (c) => `<div class="card">
  <div class="card-header">
    <div class="card-dot ${e(c.dot)}"></div>
    <h3>${e(c.title)}</h3>
  </div>
  <ul>
${c.items.map((item) => `    <li>&bull; ${e(item)}</li>`).join('\n')}
  </ul>
</div>`;
  return `<div class="cards">\n${diagram.cards.map(card).join('\n\n')}\n</div>`;
};
