// Viewer actions. Element ids are bound by src/scripts/archify/{preset,theme,export,presentation}.js.
HerdrVisual.components.toolbar = () => `<div class="toolbar" role="toolbar" aria-label="Diagram actions">
  <button id="btn-theme" type="button"
          title="Toggle theme (T)"
          aria-label="Toggle color theme"
          aria-pressed="false">
    <span id="theme-icon" aria-hidden="true">&#9790;</span>
    <span id="theme-label">Dark</span>
  </button>
  <div class="preset-wrap">
    <button id="btn-preset" type="button"
            title="Choose visual style (S cycles)"
            aria-label="Choose visual style"
            aria-haspopup="menu"
            aria-expanded="false"
            aria-controls="preset-menu"
            aria-keyshortcuts="S">
      <span class="preset-control-mark" aria-hidden="true"></span>
      <span id="preset-label">Style</span>
      <span aria-hidden="true">&#9662;</span>
    </button>
    <div class="preset-menu" id="preset-menu" role="menu" aria-label="Visual style">
      <div class="preset-menu-heading" role="presentation"><span>Visual identity</span><span>S cycles</span></div>
      <button class="preset-option" data-preset-value="classic" type="button" role="menuitemradio" aria-checked="false" tabindex="-1">
        <span class="preset-option-swatch classic" aria-hidden="true"></span>
        <span class="preset-option-copy"><strong>Classic</strong><small>Stable technical default</small></span>
        <span class="preset-option-check" aria-hidden="true">&#10003;</span>
      </button>
      <button class="preset-option" data-preset-value="signal-flow" type="button" role="menuitemradio" aria-checked="false" tabindex="-1">
        <span class="preset-option-swatch signal-flow" aria-hidden="true"></span>
        <span class="preset-option-copy"><strong>Signal Flow</strong><small>Motion-forward presentation</small></span>
        <span class="preset-option-check" aria-hidden="true">&#10003;</span>
      </button>
      <button class="preset-option" data-preset-value="blueprint" type="button" role="menuitemradio" aria-checked="false" tabindex="-1">
        <span class="preset-option-swatch blueprint" aria-hidden="true"></span>
        <span class="preset-option-copy"><strong>Blueprint</strong><small>Engineering review</small></span>
        <span class="preset-option-check" aria-hidden="true">&#10003;</span>
      </button>
      <button class="preset-option" data-preset-value="editorial" type="button" role="menuitemradio" aria-checked="false" tabindex="-1">
        <span class="preset-option-swatch editorial" aria-hidden="true"></span>
        <span class="preset-option-copy"><strong>Editorial</strong><small>Publication and launch notes</small></span>
        <span class="preset-option-check" aria-hidden="true">&#10003;</span>
      </button>
    </div>
  </div>
  <button id="btn-motion" type="button" hidden
          title="Pause motion"
          aria-label="Pause motion"
          aria-pressed="true">
    <span class="motion-control-dot" aria-hidden="true"></span>
    <span id="motion-label">Live</span>
  </button>
  <button id="btn-present" type="button"
          title="Presentation stage (F)"
          aria-label="Enter presentation stage"
          aria-pressed="false">
    <span id="present-icon" aria-hidden="true">&#9633;</span>
    <span id="present-label">Present</span>
  </button>
  <div class="export-wrap">
    <button id="btn-export" type="button"
            title="Export diagram (E)"
            aria-label="Export diagram"
            aria-haspopup="menu"
            aria-expanded="false"
            aria-controls="export-menu">
      Export <span aria-hidden="true">&#9662;</span>
    </button>
    <div class="export-menu" id="export-menu" role="menu" aria-label="Export">
      <button data-format="share-card" type="button" role="menuitem" tabindex="-1">Share Card <span class="hint">1200&times;630 PNG</span></button>
      <button data-action="route-share-card" type="button" role="menuitem" tabindex="-1" hidden disabled>Route Share Card <span class="hint">1200&times;630 PNG</span></button>
      <button data-action="reach-share-card" type="button" role="menuitem" tabindex="-1" hidden disabled>Reach Share Card <span class="hint">1200&times;630 PNG</span></button>
      <button data-action="copy-share-card" type="button" role="menuitem" tabindex="-1">Copy Share Card <span class="hint">PNG</span></button>
      <hr role="separator">
      <button data-action="copy"  type="button" role="menuitem" tabindex="-1">Copy to clipboard <span class="hint">PNG</span></button>
      <hr role="separator">
      <button data-format="png"   type="button" role="menuitem" tabindex="-1">Download PNG</button>
      <button data-format="jpeg"  type="button" role="menuitem" tabindex="-1">Download JPEG</button>
      <button data-format="webp"  type="button" role="menuitem" tabindex="-1">Download WebP</button>
      <hr role="separator">
      <button data-format="svg"   type="button" role="menuitem" tabindex="-1">Download SVG <span class="hint">vector</span></button>
      <button data-format="webm"  type="button" role="menuitem" tabindex="-1">Download WebM <span class="hint">6s motion</span></button>
    </div>
  </div>
</div>`;
