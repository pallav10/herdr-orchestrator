// The diagram SVG plus the viewer overlays (finder, route probe, lens, radar, guide, zoom controls) that act on it.
HerdrVisual.components.diagramStage = (diagram) => `<div class="diagram-container" data-detail-level="read">
${diagram.svg}
<p class="intent-trace-status no-print" id="intent-trace-status" role="status" aria-live="polite" aria-atomic="true"></p>
<div class="share-chapter-cue no-print" id="share-chapter-cue" hidden role="status" aria-live="polite">
  <div class="share-chapter-index">
    <span class="share-chapter-state" id="share-chapter-state">Ready</span>
    <span class="share-chapter-count" id="share-chapter-count">Chapter 01 / 01</span>
  </div>
  <div class="share-chapter-copy">
    <strong id="share-chapter-label">Guided chapter</strong>
    <small id="share-chapter-note"></small>
  </div>
  <span class="share-chapter-route" id="share-chapter-route"></span>
  <span class="share-chapter-progress" aria-hidden="true"><span id="share-chapter-progress-bar"></span></span>
</div>
<div class="diagram-guide no-print" id="diagram-guide" hidden role="dialog" aria-modal="false" aria-labelledby="diagram-guide-title">
  <div class="diagram-guide-head">
    <span>
      <span class="diagram-guide-eyebrow">Diagram guide</span>
      <strong class="diagram-guide-title" id="diagram-guide-title">Explore this system</strong>
    </span>
    <button class="diagram-guide-close" id="diagram-guide-close" type="button" aria-label="Close diagram guide">&#215;</button>
  </div>
  <p class="diagram-guide-stats" id="diagram-guide-stats">Inspecting compiled semantics</p>
  <div class="diagram-guide-actions" id="diagram-guide-actions" role="group" aria-label="Diagram exploration actions">
    <button class="diagram-guide-action" type="button" data-guide-action="find" aria-label="Find any node">
      <span class="diagram-guide-index" aria-hidden="true">01</span>
      <span class="diagram-guide-action-copy"><strong>Find any node</strong><small>Search labels, responsibilities, kinds, and stable IDs.</small></span>
      <kbd aria-hidden="true">/</kbd>
    </button>
    <button class="diagram-guide-action" type="button" data-guide-action="route" aria-label="Trace a directed route">
      <span class="diagram-guide-index" aria-hidden="true">02</span>
      <span class="diagram-guide-action-copy"><strong>Trace a route</strong><small>Ask how two semantic nodes connect in authored direction.</small></span>
      <kbd aria-hidden="true">R</kbd>
    </button>
    <button class="diagram-guide-action" type="button" data-guide-action="map" aria-label="See the whole system">
      <span class="diagram-guide-index" aria-hidden="true">03</span>
      <span class="diagram-guide-action-copy"><strong>See the whole system</strong><small>Open Semantic Radar with a live viewport and stable nodes.</small></span>
      <kbd aria-hidden="true">M</kbd>
    </button>
    <button class="diagram-guide-action" type="button" data-guide-action="lens" aria-label="Compare semantic kinds">
      <span class="diagram-guide-index" aria-hidden="true">04</span>
      <span class="diagram-guide-action-copy"><strong>Compare semantic kinds</strong><small>Count roles, reveal their traffic, and compare direct authored links.</small></span>
      <kbd aria-hidden="true">L</kbd>
    </button>
    <button class="diagram-guide-action" type="button" data-guide-action="story" aria-label="Play the guided story">
      <span class="diagram-guide-index" aria-hidden="true">05</span>
      <span class="diagram-guide-action-copy"><strong>Play the guided story</strong><small id="diagram-guide-story-copy">Walk the authored chapters and real relationships.</small></span>
      <kbd aria-hidden="true">P</kbd>
    </button>
    <button class="diagram-guide-action" type="button" data-guide-action="present" aria-label="Enter presentation stage">
      <span class="diagram-guide-index" aria-hidden="true">06</span>
      <span class="diagram-guide-action-copy"><strong>Enter Presentation Stage</strong><small>Give the live diagram the viewport without changing export.</small></span>
      <kbd aria-hidden="true">F</kbd>
    </button>
  </div>
  <div class="diagram-guide-shortcuts" aria-label="Additional keyboard shortcuts">
    <span><kbd>E</kbd> Export</span><span><kbd>T</kbd> Theme</span><span><kbd>S</kbd> Style</span><span><kbd>0</kbd> Reset</span><span><kbd>+</kbd> Zoom in</span><span><kbd>-</kbd> Zoom out</span><span><kbd>Esc</kbd> Close</span>
  </div>
  <p class="diagram-guide-feedback" id="diagram-guide-feedback" aria-live="polite"></p>
</div>
<div class="node-finder no-print" id="node-finder" hidden role="dialog" aria-modal="false" aria-labelledby="node-finder-title">
  <div class="node-finder-head">
    <strong id="node-finder-title">Find a node</strong>
    <button class="node-finder-close" id="node-finder-close" type="button" aria-label="Close node finder">&#215;</button>
  </div>
  <div class="node-finder-search">
    <span aria-hidden="true">&#8981;</span>
    <input class="node-finder-input" id="node-finder-input" type="search" autocomplete="off" spellcheck="false" placeholder="Search labels or IDs" aria-label="Search diagram nodes" aria-describedby="node-finder-status">
    <kbd aria-hidden="true">/</kbd>
  </div>
  <div class="node-finder-results" id="node-finder-results" role="group" aria-label="Diagram nodes"></div>
  <p class="node-finder-empty" id="node-finder-empty" hidden>No matching nodes</p>
  <p class="node-finder-status" id="node-finder-status" aria-live="polite"></p>
</div>
<div class="focus-chip relationship-lens no-print" id="focus-chip" hidden role="region" aria-labelledby="relationship-lens-title">
  <div class="relationship-lens-head">
    <div class="relationship-lens-copy">
      <span class="relationship-lens-eyebrow">Semantic passport</span>
      <strong class="relationship-lens-title" id="relationship-lens-title"><span id="focus-label"></span></strong>
      <span class="semantic-passport-detail" id="focus-detail" hidden></span>
      <div class="semantic-passport-meta" id="focus-passport-meta" aria-label="Node metadata">
        <span id="focus-kind" data-passport="kind"></span>
        <span id="focus-context" data-passport="context" hidden></span>
        <span id="focus-tag" data-passport="tag" hidden></span>
        <code id="focus-id" data-passport="id"></code>
      </div>
      <div class="semantic-passport-evidence" id="focus-evidence" hidden aria-label="Verified source evidence">
        <div class="semantic-passport-evidence-head">
          <span class="semantic-passport-evidence-status">Verified source</span>
          <a class="semantic-passport-repository" id="focus-repository" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer"></a>
        </div>
        <div class="semantic-passport-evidence-links" id="focus-evidence-links"></div>
      </div>
      <span class="relationship-lens-summary" id="focus-summary" aria-live="polite"></span>
      <div class="semantic-passport-reach" id="focus-reach" hidden>
        <span class="semantic-passport-reach-label">Authored reach</span>
        <div class="semantic-passport-reach-actions" role="group" aria-label="Trace authored reachability">
          <button id="btn-reach-upstream" type="button" aria-label="Trace upstream authored reachability" aria-pressed="false">
            <span>Upstream</span><strong id="focus-reach-upstream-count">0</strong>
          </button>
          <button id="btn-reach-downstream" type="button" aria-label="Trace downstream authored reachability" aria-pressed="false">
            <span>Downstream</span><strong id="focus-reach-downstream-count">0</strong>
          </button>
        </div>
        <small class="semantic-passport-reach-status" id="focus-reach-status" hidden aria-live="polite"></small>
      </div>
    </div>
    <div class="relationship-lens-actions">
      <button id="btn-focus-clear" type="button" aria-label="Close semantic passport" title="Close">&#215;</button>
      <button id="btn-focus-copy" type="button" aria-label="Copy link to focused node">Copy link</button>
      <button id="btn-focus-relations" type="button" aria-label="Show connected relationships" aria-expanded="false" aria-controls="relationship-lens-list">Relations</button>
    </div>
  </div>
  <div class="relationship-lens-list" id="relationship-lens-list" aria-label="Connected relationships"></div>
</div>
<div class="route-probe no-print" id="route-probe" hidden role="region" aria-labelledby="route-probe-title" data-state="idle">
  <div class="route-probe-head">
    <span class="route-probe-copy">
      <span class="route-probe-eyebrow">Route probe</span>
      <strong class="route-probe-title" id="route-probe-title">Choose a start node</strong>
    </span>
    <span class="route-probe-actions">
      <button class="route-probe-find" id="route-probe-find" type="button" aria-label="Find a route start" data-node-finder-trigger>Find start</button>
      <button class="route-probe-copy-link" id="route-probe-copy" type="button" aria-label="Copy link to traced route" hidden>Copy link</button>
      <button id="route-probe-clear" type="button" aria-label="Clear route probe">Clear</button>
    </span>
  </div>
  <div class="route-probe-path" id="route-probe-path" aria-label="Traced route">
    <span class="route-probe-placeholder">Pick two semantic nodes on the diagram</span>
  </div>
  <div class="route-journey-controls" id="route-journey-controls" hidden role="group" aria-label="Route journey controls" data-playing="false">
    <button id="route-journey-prev" type="button" aria-label="Previous route position" title="Previous route position">&#8592;</button>
    <button class="route-journey-play" id="route-journey-play" type="button" aria-label="Play route journey" aria-pressed="false" title="Play route journey">
      <span id="route-journey-play-icon" aria-hidden="true">&#9654;</span>
      <span id="route-journey-play-label">Journey</span>
    </button>
    <button id="route-journey-next" type="button" aria-label="Next route position" title="Next route position">&#8594;</button>
    <button class="route-journey-overview" id="route-journey-overview" type="button" aria-label="Show complete route overview" title="Show complete route overview">Overview</button>
  </div>
  <small class="route-probe-status" id="route-probe-status" aria-live="polite">Choose the source, then the destination. Direction matters.</small>
</div>
<div class="semantic-lens no-print" id="semantic-lens" hidden role="dialog" aria-modal="false" aria-labelledby="semantic-lens-title">
  <div class="semantic-lens-head">
    <span>
      <span class="semantic-lens-eyebrow">Semantic lens</span>
      <strong class="semantic-lens-title" id="semantic-lens-title">Compare system roles</strong>
    </span>
    <button class="semantic-lens-close" id="semantic-lens-close" type="button" aria-label="Close semantic lens">&#215;</button>
  </div>
  <p class="semantic-lens-instruction">Choose up to two semantic kinds. One reveals its real traffic; two compare only direct authored relationships.</p>
  <div class="semantic-lens-kinds" id="semantic-lens-kinds" role="group" aria-label="Semantic kinds"></div>
  <p class="semantic-lens-status" id="semantic-lens-status" aria-live="polite">Choose a kind to inspect its nodes and touching relationships.</p>
  <div class="semantic-lens-actions">
    <button id="semantic-lens-copy" type="button" aria-label="Copy link to semantic lens" hidden>Copy link</button>
    <button id="semantic-lens-clear" type="button" aria-label="Clear semantic lens">Clear</button>
  </div>
</div>
<div class="overview-map no-print" id="overview-map" hidden role="region" aria-labelledby="overview-map-title">
  <div class="overview-map-head">
    <span class="overview-map-live" aria-hidden="true"></span>
    <span class="overview-map-copy">
      <strong id="overview-map-title">Semantic radar</strong>
      <small id="overview-map-status" aria-live="polite">Building overview</small>
    </span>
    <button class="overview-map-close" id="overview-map-close" type="button" aria-label="Close semantic radar">&#215;</button>
  </div>
  <div class="overview-map-surface" id="overview-map-surface" tabindex="0" role="group" aria-label="Diagram overview. Click a node to focus it, or use arrow keys to pan."></div>
  <div class="overview-map-hint" aria-hidden="true"><span>Click node</span><span>Drag to pan</span></div>
</div>
<div class="diagram-nav no-print" role="toolbar" aria-label="Diagram view controls">
  <button id="btn-route-probe" type="button" aria-label="Trace a directed route" aria-pressed="false" aria-controls="route-probe" title="Trace route (R)">PATH</button>
  <button id="btn-overview-map" type="button" aria-label="Open semantic radar" aria-expanded="false" aria-controls="overview-map" title="Semantic radar (M)">MAP</button>
  <button id="btn-semantic-lens" type="button" aria-label="Open semantic lens" aria-haspopup="dialog" aria-expanded="false" aria-pressed="false" aria-controls="semantic-lens" title="Semantic lens (L)">LENS</button>
  <button id="btn-node-finder" type="button" aria-label="Find a node" aria-haspopup="dialog" aria-expanded="false" aria-controls="node-finder" title="Find a node (/)" data-node-finder-trigger>&#8981;</button>
  <button id="btn-diagram-guide" type="button" aria-label="Open diagram guide" aria-haspopup="dialog" aria-expanded="false" aria-controls="diagram-guide" title="Diagram guide (?)">?</button>
  <button data-view="out" type="button" aria-label="Zoom out" title="Zoom out (-)">&#8722;</button>
  <button data-view="reset" type="button" aria-label="Reset diagram view" title="Reset view (0)">100%</button>
  <button data-view="in" type="button" aria-label="Zoom in" title="Zoom in (+)">+</button>
</div>
</div>`;
