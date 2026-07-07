<script>
  import FormVisual from "./FormVisual.svelte";
  import { playAudio, roundAudioSrc } from "$lib/audio.svelte.js";

  // `round` is the latest round payload from the game WS; `reveal` (when
  // set) is the reveal payload + the player's own answer.
  let { round, reveal, yourAnswer } = $props();

  let now = $state(Date.now());
  $effect(() => {
    if (round && round.state === "active" && round.opened_at && round.duration_s > 0) {
      const t = setInterval(() => { now = Date.now(); }, 250);
      return () => clearInterval(t);
    }
  });

  const msLeft = $derived(
    round && round.opened_at && round.duration_s > 0
      ? Math.max(0, (round.opened_at + round.duration_s) * 1000 - now)
      : 0
  );
  const secsLeft = $derived(Math.ceil(msLeft / 1000));
  const pctLeft = $derived(
    round && round.duration_s > 0
      ? Math.min(100, (msLeft / (round.duration_s * 1000)) * 100)
      : 0
  );

  const winners = $derived(new Set(reveal?.winning_zones || []));
  const tally = $derived(reveal?.tally || {});
  const revealResult = $derived.by(() => {
    if (!reveal) return null;
    if (!yourAnswer || !yourAnswer.zone) {
      return { text: "Keine Position erfasst", cls: "absent" };
    }
    const opt = (reveal.options || []).find((o) => o.zone === yourAnswer.zone);
    return {
      text: `Erfasst: ${opt ? opt.label : yourAnswer.zone}`,
      cls: winners.has(yourAnswer.zone) ? "win" : "",
    };
  });
</script>

<div class="round-panel">
  {#if reveal && reveal.round_type !== "narration"}
    <div class="question">{reveal.question}</div>
    <div class="options">
      {#each reveal.options || [] as o (o.zone)}
        <div class="option" class:winner={winners.has(o.zone)}>
          {o.label}<span class="count">{tally[o.zone] || 0}</span>
        </div>
      {/each}
    </div>
    <div class="result {revealResult.cls}">{revealResult.text}</div>
  {:else if !round}
    <div class="phase-note">Warte auf den nächsten Schritt…</div>
  {:else if round.round_type === "narration"}
    <div class="question">{round.question}</div>
    <div class="step-text narration">{round.text || ""}</div>
    {#if round.audio_url}
      <div class="replay-row">
        <button class="replay" onclick={() => playAudio(roundAudioSrc(round))}>↺ Nochmal hören</button>
      </div>
    {/if}
    <div class="listen-note">Hör zu — es geht gleich weiter.</div>
  {:else}
    <div class="question">{round.question}</div>
    {#if round.text}<div class="step-text">{round.text}</div>{/if}
    <FormVisual {round} />
    {#if round.audio_url}
      <div class="replay-row">
        <button class="replay" onclick={() => playAudio(roundAudioSrc(round))}>↺ Nochmal hören</button>
      </div>
    {/if}
    <div class="countdown-wrap">
      {#if round.state === "active" && round.opened_at && round.duration_s > 0}
        <div class="countdown">
          {#if secsLeft > 0}<b>{secsLeft}s</b> — finde deine Position{:else}Die Zeit ist um!{/if}
        </div>
        <div class="timebar">
          <div class="timebar-fill" class:low={msLeft < 5000} style="width: {pctLeft}%"></div>
        </div>
      {:else if round.state === "closing"}
        <div class="locking">Positionen werden gespeichert…</div>
      {/if}
    </div>
  {/if}
</div>
