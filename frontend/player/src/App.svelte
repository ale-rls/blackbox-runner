<script>
  import ClaimForm from "./components/ClaimForm.svelte";
  import RoundPanel from "./components/RoundPanel.svelte";
  import { audio, attachElement, unlockAudio, playAudio } from "./lib/audio.svelte.js";
  import { connectPocketBase } from "./lib/pb.js";

  const playerId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop());

  let player = $state(null);
  let round = $state(null);       // latest round payload from the game WS
  let reveal = $state(null);      // reveal payload once the round is revealed
  let yourAnswer = $state(null);
  let score = $state(0);
  let ritual = $state(false);
  let narrationEl = $state(null);
  let wasBound = false;

  const bound = $derived(player?.state === "bound");
  const lostOrOrphaned = $derived(player?.state === "lost" || player?.state === "orphaned");

  $effect(() => { if (narrationEl) attachElement(narrationEl); });

  function applyPlayer(p) {
    player = p;
    if (p.state === "bound") {
      ritual = false; // rebound: ritual (if any) is resolved
      if (!wasBound) connectGameWs();
      wasBound = true;
    }
  }

  async function claim(gid) {
    unlockAudio(); // the tap that claims is also the tap that unlocks audio
    const resp = await fetch(`/api/players/${encodeURIComponent(playerId)}/claim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gid }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || `Verbindung fehlgeschlagen (${resp.status})`);
    }
    applyPlayer(await resp.json());
  }

  async function poll() {
    try {
      const resp = await fetch(`/api/players/${encodeURIComponent(playerId)}`);
      if (resp.ok) applyPlayer(await resp.json());
    } catch {
      // network hiccup: keep last known UI, try again next tick
    }
  }

  function connectGameWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/player/${encodeURIComponent(playerId)}`);
    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.type === "hello") {
        round = msg.round;
        reveal = null;
        // Joining mid-step: catch up on the running narration/question audio.
        if (msg.round && msg.round.state === "active") playAudio(msg.round.audio_url);
        if (msg.scores && msg.scores[playerId] !== undefined) score = msg.scores[playerId];
      } else if (msg.type === "round_opened") {
        round = msg;
        reveal = null;
        yourAnswer = null;
        playAudio(msg.audio_url);
      } else if (msg.type === "round_closing" || msg.type === "answers_locked") {
        round = msg;
      } else if (msg.type === "reveal") {
        yourAnswer = msg.your_answer;
        reveal = msg;
      } else if (msg.type === "scores_updated") {
        score = msg.your_score || 0;
      } else if (msg.type === "ritual_prompt" && msg.player_id === playerId) {
        ritual = true;
      }
    };
    ws.onclose = () => setTimeout(() => { if (wasBound) connectGameWs(); }, 1500);
  }

  // PocketBase realtime (issue #17): score badge fed by the public
  // score_events collection; round state kept honest straight from the
  // public rounds collection even if the game WS drops for a moment.
  connectPocketBase({
    playerId,
    onScore: (total) => { score = total; },
    onRoundRecord: (record, action) => {
      if (round && record.question_id === round.round_id && !reveal) {
        if (record.state === "closing" && round.state === "active") {
          round = { ...round, state: "closing" };
        }
      }
    },
  }).catch(() => {
    // PocketBase realtime is an enhancement; the game WS remains the
    // primary channel, so a subscribe failure must never break the page.
  });

  poll();
  setInterval(poll, 3000);
</script>

{#if bound}
  <div class="topbar">
    <span class="gid-chip">#{player.gid}</span>
    <span class="conn bound">Verbunden</span>
    {#if score}<span class="score-chip">{score} Punkte</span>{/if}
  </div>
{/if}

{#if audio.overlayVisible}
  <div id="audio-unlock" onclick={unlockAudio} role="button" tabindex="0"
       onkeydown={(e) => { if (e.key === "Enter") unlockAudio(); }}>
    <div class="icon">🔊</div>
    <div class="label">Tippen, um den Ton zu starten</div>
    <div class="sub">Setz deine Kopfhörer auf</div>
  </div>
{/if}

<main>
  {#if ritual && !bound}
    <div class="banner ritual">Geh zur leuchtenden Ecke ✦</div>
  {/if}
  {#if lostOrOrphaned}
    <div class="banner lost">Wir haben dich verloren — gib deine neue Nummer ein oder warte auf das Personal</div>
  {/if}

  {#if !bound}
    <ClaimForm {playerId} onclaim={claim} />
  {:else}
    <RoundPanel {round} {reveal} {yourAnswer} />
  {/if}
</main>

<audio bind:this={narrationEl} id="narration" preload="auto"></audio>
