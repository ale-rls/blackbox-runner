// Narration audio with the browser-gesture unlock dance, ported from
// web/player/index.html: the claim tap doubles as the unlock gesture;
// players who reconnect already-bound never tap anything, so they get the
// overlay instead.
import { gameUrl } from "$lib/config.js";
import { pbFileUrl } from "$lib/pb.js";

// 44-byte silent WAV: a playable src for the unlock gesture.
const SILENCE =
  "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";

/**
 * A round payload's narration audio source. Prefers the PocketBase-stored
 * file (built with the SDK's pb.files.getURL via the payload's audio_file
 * ref); falls back to the game-served /audio URL when the file isn't in
 * PocketBase or the PB connection isn't up yet.
 */
export function roundAudioSrc(round) {
  if (!round) return null;
  return pbFileUrl(round.audio_file) || round.audio_url;
}

export const audio = $state({
  unlocked: false,
  overlayVisible: false,
  pendingUrl: null,
});

let el = null;

export function attachElement(audioEl) {
  el = audioEl;
}

export function unlockAudio() {
  if (audio.unlocked) return;
  audio.unlocked = true;
  audio.overlayVisible = false;
  if (audio.pendingUrl) {
    playAudio(audio.pendingUrl);
  } else if (el) {
    el.src = SILENCE;
    el.play().catch(() => {});
  }
}

export function playAudio(url) {
  if (!url || !el) return;
  if (!audio.unlocked) {
    audio.pendingUrl = url;
    audio.overlayVisible = true;
    return;
  }
  audio.pendingUrl = null;
  el.src = gameUrl(url); // audio_url is game-server-relative (/audio/x.mp3)
  el.play().catch((err) => {
    // AbortError just means a newer step's audio superseded this play()
    // mid-load — the newer one is already playing, nothing to do.
    if (err && err.name === "AbortError") return;
    // Playback was blocked after all (e.g. iOS revoked the unlock):
    // fall back to an explicit tap.
    audio.unlocked = false;
    audio.pendingUrl = url;
    audio.overlayVisible = true;
  });
}
