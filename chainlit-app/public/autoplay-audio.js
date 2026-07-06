(() => {
  const RESPONSE_AUDIO_LABEL = "ฟังเสียงตอบกลับ";
  const RESPONSE_AUDIO_FILE_PREFIX = "ai_response_";
  const playedAudios = new WeakSet();
  const attemptedAudios = new WeakSet();
  const pendingAudios = new Set();

  function audioLooksLikeResponse(audio) {
    const nearby = audio.closest('[data-testid], [class*="audio"], [class*="element"], li, div');
    const nearbyText = nearby ? nearby.textContent || "" : "";
    const audioMeta = [
      audio.getAttribute("aria-label"),
      audio.getAttribute("title"),
      audio.getAttribute("src"),
      audio.currentSrc,
      audio.src,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      nearbyText.includes(RESPONSE_AUDIO_LABEL) ||
      audioMeta.includes(RESPONSE_AUDIO_LABEL) ||
      audioMeta.includes(RESPONSE_AUDIO_FILE_PREFIX)
    );
  }

  function tryPlay(audio, fromUserGesture = false) {
    if (!(audio instanceof HTMLAudioElement)) return;
    if (playedAudios.has(audio) || !audioLooksLikeResponse(audio)) return;
    if (attemptedAudios.has(audio) && !fromUserGesture) return;

    attemptedAudios.add(audio);
    audio.autoplay = true;
    audio.controls = true;

    const playResult = audio.play();
    if (playResult && typeof playResult.then === "function") {
      playResult
        .then(() => {
          playedAudios.add(audio);
          pendingAudios.delete(audio);
        })
        .catch(() => {
          pendingAudios.add(audio);
        });
    } else {
      playedAudios.add(audio);
      pendingAudios.delete(audio);
    }
  }

  function scanForResponseAudio(root = document) {
    if (root instanceof HTMLAudioElement) {
      tryPlay(root);
      return;
    }

    if (!root.querySelectorAll) return;
    root.querySelectorAll("audio").forEach((audio) => {
      audio.addEventListener("loadedmetadata", () => tryPlay(audio), { once: true });
      tryPlay(audio);
    });
  }

  function retryPendingAudio() {
    pendingAudios.forEach((audio) => {
      pendingAudios.delete(audio);
      if (document.contains(audio)) {
        tryPlay(audio, true);
      }
    });
  }

  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      mutation.addedNodes.forEach((node) => scanForResponseAudio(node));
    });
  });

  scanForResponseAudio();
  observer.observe(document.body, { childList: true, subtree: true });

  ["click", "keydown", "touchstart"].forEach((eventName) => {
    window.addEventListener(eventName, retryPendingAudio, { passive: true });
  });
})();
