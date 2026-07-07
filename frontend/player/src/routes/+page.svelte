<script>
  // Entry point for "send everyone the same link": visiting / assigns a
  // fresh seat id and redirects to that player page. The id sticks in
  // localStorage, so re-opening the link (or a reload that lands here)
  // returns the same seat instead of minting a new identity mid-show.
  import { goto } from "$app/navigation";
  import { onMount } from "svelte";

  onMount(() => {
    let id = localStorage.getItem("blackbox-player-id");
    if (!id) {
      const suffix = crypto.randomUUID
        ? crypto.randomUUID().replace(/-/g, "").slice(0, 6)
        : Math.random().toString(36).slice(2, 8);
      id = `seat-${suffix}`;
      localStorage.setItem("blackbox-player-id", id);
    }
    goto(`/p/${encodeURIComponent(id)}`, { replaceState: true });
  });
</script>

<main>
  <div class="phase-note">Einen Moment…</div>
</main>
