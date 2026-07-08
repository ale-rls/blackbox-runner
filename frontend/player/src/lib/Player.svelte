<script>
  import ClaimForm from "$lib/components/ClaimForm.svelte";
  import RoundPanel from "$lib/components/RoundPanel.svelte";
  import { audio, attachElement, unlockAudio, playAudio, roundAudioSrc } from "$lib/audio.svelte.js";
  import { connectPlayer, submitClaim } from "$lib/pb.js";
  import { onMount } from "svelte";

  let { playerId } = $props();

  let player = $state(null);
  let round = $state(null);       // latest round payload (the public rounds.payload)
  let reveal = $state(null);      // same payload once the round is revealed (tally + winners)
  let yourAnswer = $state(null);
  let score = $state(0);
  let available = $state(null);   // claimable GIDs; null until game_state loads
  let narrationEl = $state(null);

  // Which round's narration/question audio we've already started, so a fresh
  // active round (or binding into one already running) plays exactly once.
  let playedRoundId = null;

  const bound = $derived(player?.state === "bound");
  const lostOrOrphaned = $derived(player?.state === "lost" || player?.state === "orphaned");
  // The ritual prompt ("walk to the glowing corner") is a cue the deployed
  // phone can't receive, but a player is asked to do the ritual exactly when
  // they orphan — so derive it from the persisted state instead.
  const ritual = $derived(player?.state === "orphaned");

  $effect(() => { if (narrationEl) attachElement(narrationEl); });

  function playRoundOnce(payload) {
    if (payload && payload.state === "active" && playedRoundId !== payload.round_id) {
      playedRoundId = payload.round_id;
      playAudio(roundAudioSrc(payload));
    }
  }

  function applyPlayer(p) {
    const wasBound = player?.state === "bound";
    player = p;
    // Just bound into an already-running round: catch up on its audio.
    if (p.state === "bound" && !wasBound) playRoundOnce(round);
  }

  function applyRound(payload) {
    if (payload.round_id !== round?.round_id) {
      yourAnswer = null; // new round starting
      playedRoundId = null;
    }
    round = payload;
    reveal = payload.state === "revealed" ? payload : null;
    if (bound) playRoundOnce(payload);
  }

  async function claim(gid) {
    unlockAudio(); // the tap that claims is also the tap that unlocks audio
    // submitClaim resolves once the server has bound us; the players
    // subscription then flips `player` to bound and swaps in the round panel.
    await submitClaim(playerId, gid);
  }

  onMount(() => {
    connectPlayer({
      playerId,
      onPlayer: applyPlayer,
      onRound: applyRound,
      onYourAnswer: (a) => { yourAnswer = a; },
      onScore: (total) => { score = total; },
      onAvailable: (gids) => { available = gids; },
    }).catch(() => {
      // Nothing to fall back to — PocketBase is the only backend now — but a
      // connect failure must not throw out of onMount and blank the page.
    });
  });
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
    <ClaimForm {playerId} {available} onclaim={claim} />
  {:else}
    <RoundPanel {round} {reveal} {yourAnswer} />
  {/if}
</main>

<audio bind:this={narrationEl} id="narration" preload="auto"></audio>
