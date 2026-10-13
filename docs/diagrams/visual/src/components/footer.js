HerdrVisual.components.footer = (diagram) => {
  const type = diagram.type;
  const label = type.charAt(0).toUpperCase() + type.slice(1);
  const views = diagram.views.length ? ' &bull; <kbd>[</kbd>/<kbd>]</kbd> views &bull; <kbd>P</kbd> play story' : '';
  return `<p class="footer">
  ${label} diagram &bull; Built with Archify<span class="no-print"> &bull; <a class="artifact-start-link" href="https://tt-a1i.github.io/archify/start.html?type=${type}&amp;source=artifact" target="_blank" rel="noopener noreferrer">Create yours &nearr;</a> &bull; Hover to trace &bull; <kbd>R</kbd> route &bull; Click to focus &bull; <kbd>+</kbd>/<kbd>&minus;</kbd> zoom &bull; <kbd>M</kbd> radar${views} &bull; <kbd>T</kbd> theme &bull; <kbd>E</kbd> export</span>
</p>`;
};
