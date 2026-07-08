<script>
  import { page } from "$app/state";
  import Player from "$lib/Player.svelte";

  const playerId = $derived(decodeURIComponent(page.params.player_id));

  // An usher sending someone straight to /p/seat-12 also makes that their
  // sticky seat, so the shared root link brings them back here later.
  $effect(() => {
    localStorage.setItem("blackbox-player-id", playerId);
  });
</script>

{#key playerId}
  <Player {playerId} />
{/key}
