// Chapter data plus the empty story rail; src/scripts/archify/guided-views.js renders chapters from the data.
HerdrVisual.components.guidedViews = (diagram) => {
  const data = JSON.stringify(diagram.views).replace(/</g, '\\u003c');
  return `<script id="archify-guided-views-data" type="application/json">${data}</script>
<div class="guided-views no-print" id="guided-views" hidden aria-label="Guided diagram views">
  <button id="guided-view-prev" type="button" aria-label="Previous guided view" title="Previous guided view ([)">&#8592;</button>
  <div class="guided-view-copy">
    <div class="guided-view-meta">
      <span>Guided views</span>
      <span id="guided-view-count"></span>
      <span class="guided-view-progress" aria-hidden="true"><span id="guided-view-progress-bar"></span></span>
      <span class="guided-view-handoff" id="guided-view-handoff" hidden aria-hidden="true"></span>
    </div>
    <strong id="guided-view-label">Explore this system</strong>
    <small id="guided-view-note" aria-live="polite" aria-atomic="true">Step through curated paths without changing the source diagram.</small>
    <div class="guided-view-trail" id="guided-view-trail" hidden role="group" aria-label="Story trail"></div>
    <div class="guided-story-caption" id="guided-story-caption" hidden aria-live="polite" aria-atomic="true">
      <span class="guided-story-caption-index" id="guided-story-caption-index">Beat</span>
      <strong id="guided-story-caption-route"></strong>
      <small id="guided-story-caption-detail"></small>
      <span class="guided-story-caption-next" id="guided-story-caption-next" hidden aria-hidden="true">
        <small>Next</small>
        <strong id="guided-story-caption-next-label"></strong>
      </span>
    </div>
  </div>
  <button id="guided-view-next" type="button" aria-label="Next guided view" title="Next guided view (])">&#8594;</button>
  <div class="guided-view-actions">
    <button class="guided-view-play" id="guided-view-play" type="button" aria-label="Play guided story" aria-pressed="false" title="Play guided story (P)">
      <span id="guided-view-play-icon" aria-hidden="true">&#9654;</span>
      <span id="guided-view-play-label">Play story</span>
    </button>
    <button class="guided-view-beat-link" id="guided-view-beat-link" type="button" aria-label="Select a Story Beat to copy its exact link" title="Select a Story Beat to copy its exact link" disabled>
      <span aria-hidden="true">&#35;</span>
      <span id="guided-view-beat-link-label">Copy moment</span>
    </button>
    <button class="guided-view-all" id="guided-view-all" type="button" aria-label="Show entire diagram">Show all</button>
  </div>
  <nav class="guided-view-index" id="guided-view-index" aria-label="Story chapters">
    <ol class="guided-view-chapters" id="guided-view-chapters"></ol>
  </nav>
</div>`;
};
