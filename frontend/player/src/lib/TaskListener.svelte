<script>
  import RoundPanel from "$lib/components/RoundPanel.svelte";
  import { audio, attachElement, unlockAudio, playAudio, roundAudioSrc } from "$lib/audio.svelte.js";
  import { gameFetch, gameWsUrl } from "$lib/config.js";
  import { ensurePocketbaseUrl } from "$lib/pb.js";
  import { onMount } from "svelte";

  let round = $state(null);
  let reveal = $state(null);
  let zoneCounts = $state({});
  let scores = $state({});
  let zones = $state(null);
  let connected = $state(false);
  let lastCue = $state("waiting");
  let lastUpdatedAt = $state(null);
  let narrationEl = $state(null);
  let ws = null;
  let reconnectTimer = null;
  let reconnectEnabled = false;
  let autoPlayedRoundId = null;

  $effect(() => {
    if (!narrationEl) return;
    attachElement(narrationEl);
    if (round?.state === "active") syncAudio(round);
  });

  const winningZones = $derived(new Set(reveal?.winning_zones || []));
  const zoneRows = $derived.by(() =>
    (round?.options || []).map((opt) => {
      const count = zoneCounts[opt.zone] || 0;
      return {
        zone: opt.zone,
        label: opt.label,
        count,
        winner: winningZones.has(opt.zone),
      };
    })
  );
  const maxZoneCount = $derived.by(() => Math.max(1, ...zoneRows.map((row) => row.count)));
  const scoreRows = $derived.by(() =>
    Object.entries(scores)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 8)
  );
  const summaryRows = $derived.by(() => {
    const rows = [
      { label: "Connection", value: connected ? "Live" : "Reconnecting" },
      { label: "Last cue", value: lastCue || "waiting" },
      { label: "Round", value: round?.round_id || "Waiting for a round" },
      { label: "State", value: round?.state || "idle" },
      { label: "Type", value: round?.round_type || "n/a" },
      { label: "Step", value: round ? `#${round.index + 1}` : "—" },
      {
        label: "Duration",
        value: round ? (round.duration_s > 0 ? `${formatSeconds(round.duration_s)}` : "untimed") : "—",
      },
      {
        label: "Grace",
        value: round ? formatSeconds(round.grace_s || 0) : "—",
      },
      { label: "Opened", value: formatClock(round?.opened_at) },
      { label: "Closed", value: formatClock(round?.closed_at) },
    ];
    return rows;
  });
  const debugState = $derived.by(() => ({
    connected,
    lastCue,
    lastUpdatedAt: formatClock(lastUpdatedAt),
    audioUnlocked: audio.unlocked,
    audioOverlayVisible: audio.overlayVisible,
    round,
    reveal,
    zoneCounts,
    scores,
    zones,
  }));

  function formatClock(ts) {
    if (ts === null || ts === undefined) return "—";
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function formatSeconds(value) {
    const seconds = Math.max(0, Math.round(value || 0));
    if (seconds < 60) return `${seconds}s`;
    const mins = Math.floor(seconds / 60);
    const secs = String(seconds % 60).padStart(2, "0");
    return `${mins}m ${secs}s`;
  }

  function markActivity(type) {
    lastCue = type;
    lastUpdatedAt = Date.now() / 1000;
  }

  function syncAudio(nextRound) {
    const src = roundAudioSrc(nextRound);
    if (!src || nextRound?.state !== "active" || !narrationEl) return;
    if (autoPlayedRoundId === nextRound.round_id) return;
    autoPlayedRoundId = nextRound.round_id;
    playAudio(src);
  }

  function applyMessage(msg) {
    if (!msg?.type) return;
    markActivity(msg.type);

    if (msg.type === "hello") {
      connected = true;
      round = msg.round;
      reveal = null;
      zoneCounts = msg.zone_counts || {};
      zones = msg.zones || null;
      syncAudio(msg.round);
    } else if (msg.type === "round_opened") {
      round = msg;
      reveal = null;
      syncAudio(msg);
    } else if (msg.type === "round_closing" || msg.type === "answers_locked") {
      round = msg;
    } else if (msg.type === "reveal") {
      round = msg;
      reveal = msg;
    } else if (msg.type === "scores_updated") {
      scores = msg.scores || {};
    } else if (msg.type === "zone_counts") {
      zoneCounts = msg.counts || {};
    } else if (msg.type === "ritual_prompt") {
      // The listener route is cue-aware but not player-aware.
    }
  }

  function connect() {
    ws = new WebSocket(gameWsUrl("/ws/td"));
    ws.onopen = () => {
      connected = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };
    ws.onmessage = (evt) => {
      applyMessage(JSON.parse(evt.data));
    };
    ws.onerror = () => {
      connected = false;
    };
    ws.onclose = () => {
      connected = false;
      if (!reconnectEnabled) return;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => {
        if (reconnectEnabled) connect();
      }, 1500);
    };
  }

  onMount(() => {
    reconnectEnabled = true;

    // Operator page: skip the tap-to-unlock dance and play outright. If the
    // browser blocks autoplay anyway, playAudio re-arms the pending URL and
    // the first gesture anywhere on the page retries it.
    unlockAudio();
    const retryUnlock = () => unlockAudio();
    window.addEventListener("pointerdown", retryUnlock);
    window.addEventListener("keydown", retryUnlock);
    // Resolve PocketBase's base URL before touching any round data so
    // narration always resolves to the PocketBase file URL, not the
    // game-served /audio fallback. If it can't be resolved the fallback
    // still plays in same-origin dev.
    void ensurePocketbaseUrl()
      .catch(() => {})
      .then(() => {
        void gameFetch("/api/rounds/current")
          .then((resp) => (resp.ok ? resp.json() : null))
          .then((current) => {
            if (!current) return;
            round = current;
            if (current.state === "active") syncAudio(current);
          })
          .catch(() => {
            // The websocket is the primary live path; the fetch is only a bootstrap.
          });

        void gameFetch("/api/scores")
          .then((resp) => (resp.ok ? resp.json() : null))
          .then((currentScores) => {
            if (currentScores) scores = currentScores;
          })
          .catch(() => {
            // Scoreboard is read-only on this page; keep going without it.
          });

        connect();
      });

    return () => {
      reconnectEnabled = false;
      window.removeEventListener("pointerdown", retryUnlock);
      window.removeEventListener("keydown", retryUnlock);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = null;
      if (ws) ws.close();
    };
  });
</script>

<main class="listener-page">
  <header class="hero">
    <div>
      <div class="eyebrow">Debug route</div>
      <h1>Current task listener</h1>
      <p class="lede">
        Live round stream without a player seat. Use this to watch the show state, hear audio,
        and verify round transitions while the phone flow is offline.
      </p>
    </div>

    <div class="chips">
      <span class:live={connected} class="chip">{connected ? "Live" : "Reconnecting"}</span>
      <span class="chip">{lastCue || "waiting"}</span>
      {#if round?.round_id}
        <span class="chip mono">{round.round_id}</span>
      {/if}
    </div>
  </header>

  <section class="layout">
    <article class="panel stage">
      <RoundPanel {round} {reveal} showPersonalResult={false} />
    </article>

    <aside class="stack">
      <article class="panel">
        <div class="panel-title">Playback</div>
        <div class="meta-grid">
          {#each summaryRows.slice(0, 6) as row (row.label)}
            <div class="meta">
              <div class="meta-label">{row.label}</div>
              <div class="meta-value">{row.value}</div>
            </div>
          {/each}
        </div>

        <audio bind:this={narrationEl} id="narration" class="audio-player" controls preload="auto"></audio>

        <div class="button-row">
          <button class="ghost" onclick={() => unlockAudio()}>Enable audio</button>
          {#if round?.audio_url || round?.audio_file}
            <button class="ghost" onclick={() => playAudio(roundAudioSrc(round))}>Play current audio</button>
          {/if}
        </div>

        <div class="hint">
          Audio starts by itself when a round opens. If the browser blocks autoplay, click
          anywhere on the page (or "Enable audio") once. The native player stays visible so you
          can pause, scrub, and confirm the loaded source.
        </div>
      </article>

      <article class="panel">
        <div class="panel-title">Live state</div>
        <div class="meta-grid">
          {#each summaryRows.slice(6) as row (row.label)}
            <div class="meta">
              <div class="meta-label">{row.label}</div>
              <div class="meta-value">{row.value}</div>
            </div>
          {/each}
          <div class="meta">
            <div class="meta-label">Tracking zones</div>
            <div class="meta-value">
              {zones?.enabled ? `${zones.zones?.length || 0} active` : "disabled"}
            </div>
          </div>
          <div class="meta">
            <div class="meta-label">Audio source</div>
            <div class="meta-value">{round?.audio_url || "none"}</div>
          </div>
        </div>

        <div class="subsection-title">Zone counts</div>
        {#if zoneRows.length}
          <div class="bars">
            {#each zoneRows as row (row.zone)}
              <div class="bar-row">
                <div class="bar-info">
                  <div class="bar-label">{row.label}</div>
                  <div class="bar-zone">{row.zone}</div>
                </div>
                <div class="bar-count">{row.count}</div>
                <div class="bar-track">
                  <div
                    class:winner={row.winner}
                    class="bar-fill"
                    style={`width: ${(row.count / maxZoneCount) * 100}%`}
                  ></div>
                </div>
              </div>
            {/each}
          </div>
        {:else}
          <div class="empty-note">Zone counts appear here once a non-narration round is active.</div>
        {/if}
      </article>

      <article class="panel">
        <div class="panel-title">Scores</div>
        {#if scoreRows.length}
          <div class="scores">
            {#each scoreRows as [playerId, points] (playerId)}
              <div class="score-row">
                <div class="score-id">{playerId}</div>
                <div class="score-points">{points}</div>
              </div>
            {/each}
          </div>
        {:else}
          <div class="empty-note">No score events yet.</div>
        {/if}
      </article>

      <details class="panel raw">
        <summary>
          <div class="panel-title">Raw state</div>
          <div class="empty-note">Expand to inspect the live payloads.</div>
        </summary>
        <div class="raw-body">
          <pre>{JSON.stringify(debugState, null, 2)}</pre>
        </div>
      </details>
    </aside>
  </section>
</main>

{#if audio.overlayVisible}
  <div
    id="audio-unlock"
    onclick={unlockAudio}
    role="button"
    tabindex="0"
    onkeydown={(e) => {
      if (e.key === "Enter") unlockAudio();
    }}
  >
    <div class="icon">🔊</div>
    <div class="label">Tap to start audio</div>
    <div class="sub">Needed for autoplay on this route too</div>
  </div>
{/if}

<style>
  .listener-page {
    width: 100%;
    max-width: min(76rem, calc(100vw - 1.4rem));
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    justify-content: flex-start;
    gap: 1rem;
    padding: 1rem 0.7rem 1.3rem;
    text-align: left;
  }

  .hero {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: flex-start;
    padding: 1.05rem 1.1rem;
    border-radius: 1rem;
    background:
      radial-gradient(circle at top right, rgba(109, 116, 246, 0.18), transparent 42%),
      linear-gradient(135deg, rgba(18, 18, 27, 0.98), rgba(12, 12, 18, 0.94));
    border: 1px solid rgba(109, 116, 246, 0.28);
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.24);
  }

  .eyebrow {
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.16em;
    font-size: 0.72rem;
    font-weight: 700;
    margin-bottom: 0.4rem;
  }

  h1 {
    margin: 0;
    font-size: clamp(1.6rem, 3vw, 2.4rem);
    line-height: 1.04;
  }

  .lede {
    margin: 0.55rem 0 0;
    color: var(--muted);
    max-width: 58ch;
    line-height: 1.45;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    justify-content: flex-end;
  }

  .chip {
    border-radius: 999px;
    padding: 0.35em 0.75em;
    font-family: var(--mono);
    font-weight: 700;
    font-size: 0.85rem;
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--text);
  }

  .chip.live {
    border-color: rgba(74, 222, 128, 0.45);
    color: var(--green);
  }

  .chip.mono {
    overflow-wrap: anywhere;
  }

  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1.6fr) minmax(19rem, 0.95fr);
    gap: 1rem;
    align-items: start;
  }

  .stack {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .panel {
    background: rgba(18, 18, 27, 0.98);
    border: 1px solid var(--border);
    border-radius: 1rem;
    padding: 1rem;
    box-shadow: 0 18px 36px rgba(0, 0, 0, 0.16);
  }

  .stage {
    min-height: 26rem;
  }

  .panel-title {
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.14em;
    font-size: 0.72rem;
    font-weight: 700;
    margin-bottom: 0.8rem;
  }

  .meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
    gap: 0.65rem;
  }

  .meta {
    padding: 0.72rem 0.8rem;
    border-radius: 0.8rem;
    background: var(--panel-2);
    border: 1px solid var(--border);
  }

  .meta-label {
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.11em;
    font-size: 0.68rem;
    font-weight: 700;
  }

  .meta-value {
    margin-top: 0.35rem;
    font-size: 0.95rem;
    font-weight: 700;
    line-height: 1.3;
    overflow-wrap: anywhere;
  }

  .audio-player {
    width: 100%;
    margin-top: 0.85rem;
  }

  .button-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
    margin-top: 0.85rem;
  }

  .ghost {
    appearance: none;
    font: inherit;
    font-weight: 700;
    border-radius: 999px;
    padding: 0.58em 1em;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text);
    cursor: pointer;
  }

  .ghost:active {
    transform: scale(0.98);
  }

  .hint,
  .empty-note {
    margin-top: 0.8rem;
    color: var(--muted);
    font-size: 0.88rem;
    line-height: 1.45;
  }

  .subsection-title {
    margin-top: 1rem;
    margin-bottom: 0.75rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.12em;
    font-size: 0.7rem;
    font-weight: 700;
  }

  .bars {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .bar-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.55rem 0.75rem;
    align-items: center;
  }

  .bar-info {
    min-width: 0;
  }

  .bar-label {
    font-weight: 700;
    line-height: 1.25;
  }

  .bar-zone {
    margin-top: 0.2rem;
    color: var(--muted);
    font-family: var(--mono);
    font-size: 0.78rem;
  }

  .bar-count {
    font-family: var(--mono);
    font-weight: 700;
    color: var(--text);
  }

  .bar-track {
    grid-column: 1 / -1;
    height: 6px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.06);
    overflow: hidden;
  }

  .bar-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, var(--accent), #aab0ff);
  }

  .bar-fill.winner {
    background: linear-gradient(90deg, var(--green), #9bf7c0);
  }

  .scores {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  .score-row {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: center;
    padding: 0.68rem 0.8rem;
    border-radius: 0.75rem;
    background: var(--panel-2);
    border: 1px solid var(--border);
  }

  .score-id {
    font-family: var(--mono);
    font-size: 0.9rem;
    overflow-wrap: anywhere;
  }

  .score-points {
    font-family: var(--mono);
    font-weight: 700;
    color: var(--text);
  }

  details.panel {
    padding: 0;
  }

  details.panel > summary {
    list-style: none;
    cursor: pointer;
    padding: 1rem;
  }

  details.panel > summary::-webkit-details-marker {
    display: none;
  }

  .raw-body {
    padding: 0 1rem 1rem;
  }

  pre {
    margin: 0;
    padding: 0.85rem;
    border-radius: 0.75rem;
    background: var(--panel-2);
    border: 1px solid var(--border);
    overflow: auto;
    max-height: 26rem;
    font-size: 0.82rem;
    line-height: 1.45;
  }

  @media (max-width: 900px) {
    .layout {
      grid-template-columns: 1fr;
    }

    .hero {
      flex-direction: column;
    }

    .chips {
      justify-content: flex-start;
    }
  }

  @media (max-width: 640px) {
    .listener-page {
      padding: 0.9rem 0.5rem 1rem;
    }

    .panel,
    .hero {
      border-radius: 0.9rem;
    }

    .meta-grid {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
