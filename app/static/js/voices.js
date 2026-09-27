/* Voice Studio — прослуховування голосів (фаза F3).
 *
 * Синтез однієї еталонної фрази на клік. Результат вставляється під рядком
 * голосу; повторний клік перемикає плеєр, а не плодить нові.
 */

"use strict";

(function () {
  function setBusy(button, busy) {
    button.disabled = busy;
    if (busy) {
      button.dataset.label = button.textContent;
      button.textContent = "◌ Синтезую…";
    } else if (button.dataset.label) {
      button.textContent = button.dataset.label;
    }
  }

  async function audition(button) {
    const voiceId = button.dataset.voiceAudition;
    const engineId = button.dataset.engine || "";
    const row = document.querySelector('[data-audition-for="' + CSS.escape(voiceId) + '"]');
    if (!row) return;

    const slot = row.querySelector("[data-audition-slot]");
    row.hidden = false;

    if (slot.dataset.loadedFor === voiceId) {
      row.hidden = true;
      slot.dataset.loadedFor = "";
      slot.innerHTML = "";
      return;
    }

    setBusy(button, true);
    slot.innerHTML = '<div class="tiny muted">Синтезую еталонну фразу…</div>';

    try {
      const body = new URLSearchParams({ voice_id: voiceId, engine_id: engineId });
      const response = await fetch("/ui/voices/preview", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      });
      slot.innerHTML = await response.text();
      slot.dataset.loadedFor = voiceId;
    } catch (err) {
      slot.innerHTML = '<div class="preview-error">Не вдалося звернутися до сервера.</div>';
    } finally {
      setBusy(button, false);
    }
  }

  function boot() {
    document.querySelectorAll("[data-voice-audition]").forEach((button) => {
      button.addEventListener("click", () => audition(button));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.VSVoice = { audition };
})();
