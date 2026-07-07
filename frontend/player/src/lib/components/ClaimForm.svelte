<script>
  // `available` is the live list of claimable GIDs (null until it loads); we
  // validate the typed number against it for instant feedback, but the server
  // stays the final authority (a race is still rejected server-side).
  let { playerId, available = null, onclaim } = $props();

  let gidText = $state("");
  let error = $state("");
  let busy = $state(false);

  async function claim() {
    const gid = parseInt(gidText, 10);
    if (!Number.isFinite(gid)) {
      error = "Gib die Nummer ein, die dir gezeigt wurde.";
      return;
    }
    if (available && !available.includes(gid)) {
      error = "Diese Nummer ist gerade nicht verfügbar. Prüf deinen Punkt.";
      return;
    }
    busy = true;
    error = "";
    try {
      await onclaim(gid);
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }
</script>

<div id="claim-form">
  <h1>Willkommen in der KI-Blackbox</h1>
  <div class="player-id">Gerät {playerId}</div>
  <p class="phase-note" style="margin:0">Gib die Nummer auf deinem Punkt ein</p>
  <input
    id="gid"
    inputmode="numeric"
    pattern="[0-9]*"
    maxlength="5"
    autocomplete="off"
    bind:value={gidText}
    onkeydown={(e) => { if (e.key === "Enter") claim(); }}
  />
  <div class="error">{error}</div>
  <button class="claim" id="submit" disabled={busy} onclick={claim}>Verbinden</button>
</div>
