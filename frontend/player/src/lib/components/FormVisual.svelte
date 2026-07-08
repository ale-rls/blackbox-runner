<script>
  // On-screen mirrors of the physical floor markings (pink scale line,
  // cross axes, quadrant fields, concentric rings) — same rendering per
  // form as web/player/index.html's formHtml().
  let { round } = $props();
  const fl = $derived(round.form_labels || {});
</script>

{#if round.form === "scale"}
  <div class="form-visual">
    <div class="scale-line"></div>
    <div class="scale-labels">
      <span>{fl.left || ""}</span>
      <span>{fl.right || ""}</span>
    </div>
  </div>
{:else if round.form === "scale3"}
  <div class="form-visual">
    <div class="scale-line"></div>
    <div class="scale-labels three">
      <span>{fl.left || ""}</span>
      <span>{fl.middle || ""}</span>
      <span>{fl.right || ""}</span>
    </div>
  </div>
{:else if round.form === "cross"}
  <div class="form-visual cross">
    <div class="cross-label y">{fl.y_top || ""}</div>
    <div class="cross-mid">
      <div class="cross-label x">{fl.x_left || ""}</div>
      <div class="cross-box"><div class="axis-v"></div><div class="axis-h"></div></div>
      <div class="cross-label x">{fl.x_right || ""}</div>
    </div>
    <div class="cross-label y">{fl.y_bottom || ""}</div>
  </div>
{:else if round.form === "quadrants"}
  <div class="form-visual quadrants">
    {#each round.options || [] as o (o.zone)}
      <div class="quadrant">{o.label}</div>
    {/each}
  </div>
{:else if round.form === "rings"}
  <div class="form-visual rings">
    <div class="ring outer"><div class="ring middle"><div class="ring inner">
      <span class="ring-center-label">{fl.center || ""}</span>
    </div></div></div>
    <div class="ring-edge-label">Rand: <b>{fl.edge || ""}</b></div>
  </div>
{:else}
  <div class="options">
    {#each round.options || [] as o (o.zone)}
      <div class="option">{o.label}</div>
    {/each}
  </div>
{/if}
