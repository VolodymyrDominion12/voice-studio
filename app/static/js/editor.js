/* Voice Studio — поведінка редактора (фаза F1).
 *
 * Розподіл ролей за ADR-008:
 *   - Alpine тримає ЛИШЕ локальний стан: виділені блоки й панель масових дій;
 *   - htmx виконує часткові оновлення (прев'ю, довантаження блоків);
 *   - усе інше — тут: автозбереження, перерахунок просодії, гарячі клавіші.
 *
 * Автозбереження навмисно НЕ робимо через htmx: підміна рядка знищила б
 * фокус і позицію курсора в textarea. Тому зберігаємо через fetch і
 * оновлюємо лише чип стану (docs/FRONTEND.md, розд. 6.4).
 */

"use strict";

(function () {
  // ── Конфіг і профілі емоцій, віддані сервером ──────────────────────────
  function readJson(id, fallback) {
    const node = document.getElementById(id);
    if (!node) return fallback;
    try {
      return JSON.parse(node.textContent);
    } catch (err) {
      console.error("Не вдалося прочитати " + id, err);
      return fallback;
    }
  }

  const CONFIG = readJson("vs-config", {});
  const PROFILES = readJson("vs-profiles", {});
  const NEUTRAL = PROFILES.neutral || { speed: 1, pitch: 0, energy_db: 0, pause_after_ms: 400 };

  // ── Просодія: та сама формула, що get_prosody() у profiles.py ──────────
  const lerp = (a, b, t) => a + (b - a) * t;

  function prosody(emotion, intensity) {
    const profile = PROFILES[emotion] || NEUTRAL;
    const t = Math.max(0, Math.min(1, Number(intensity)));
    const speed = t === 1 ? profile.speed : lerp(NEUTRAL.speed, profile.speed, t);
    const pause = t === 1
      ? profile.pause_after_ms
      : Math.round(lerp(NEUTRAL.pause_after_ms, profile.pause_after_ms, t));
    const energy = t === 1 ? profile.energy_db : lerp(NEUTRAL.energy_db, profile.energy_db, t);
    return { speed, pause, energy };
  }

  function prosodyText(emotion, intensity) {
    const p = prosody(emotion, intensity);
    const speed = p.speed.toFixed(2).replace(".", ",");
    const parts = ["темп " + speed + "×"];
    if (Math.round(p.energy) !== 0) parts.push("гучність " + Math.round(p.energy) + " дБ");
    parts.push("пауза " + p.pause + " мс");
    return parts.join(", ");
  }

  // ── Сповіщення ────────────────────────────────────────────────────────
  function toast(text, kind) {
    let host = document.querySelector(".toasts");
    if (!host) {
      host = document.createElement("div");
      host.className = "toasts";
      document.body.appendChild(host);
    }
    const item = document.createElement("div");
    item.className = "toast " + (kind || "");
    item.textContent = text;
    host.appendChild(item);
    setTimeout(() => item.remove(), 4200);
  }

  // ── Рядок блоку ───────────────────────────────────────────────────────
  function rowElements(row) {
    return {
      row: row,
      id: Number(row.dataset.blockId),
      text: row.querySelector("[data-text]"),
      emotion: row.querySelector("[data-emotion-select]"),
      intensity: row.querySelector("[data-intensity-range]"),
      intensityValue: row.querySelector("[data-intensity-value]"),
      prosody: row.querySelector("[data-prosody]"),
      save: row.querySelector("[data-save-state]"),
      preview: row.querySelector("[data-preview-btn]"),
      previewSlot: row.querySelector("[data-preview-slot]"),
      revert: row.querySelector("[data-revert-btn]"),
      speak: row.querySelector("[data-speak]"),
      pick: row.querySelector("[data-pick]"),
      normalized: row.querySelector("[data-normalized-source]"),
    };
  }

  function setSaveState(el, state, message) {
    if (!el) return;
    el.dataset.saveState = state;
    el.textContent = message || {
      saved: "✓ Збережено",
      dirty: "● Незбережено",
      saving: "◌ Зберігаю…",
      error: "⚠ Не збережено",
    }[state] || "";
  }

  function stamp() {
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    return "✓ Збережено " + hh + ":" + mm;
  }

  function refreshProsody(el) {
    if (!el.prosody || !el.emotion) return;
    el.prosody.textContent = prosodyText(el.emotion.value, el.intensity ? el.intensity.value : 0.5);
    if (el.intensityValue && el.intensity) {
      el.intensityValue.textContent = Number(el.intensity.value).toFixed(1);
    }
  }

  // Черга незбережених рядків (для Ctrl+S і перед прев'ю)
  const dirtyRows = new Set();

  async function patchBlock(blockId, payload) {
    const url = "/api/v1/documents/" + CONFIG.docId + "/blocks/" + blockId;
    const response = await fetch(url, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      throw new Error("HTTP " + response.status + ": " + (await response.text()).slice(0, 200));
    }
    return response.json();
  }

  async function saveRow(el, { silent } = {}) {
    if (!el.dirty) return true;
    if (el.dirty.timer) {
      clearTimeout(el.dirty.timer);
      el.dirty.timer = null;
    }
    el.dirty = false;
    setSaveState(el.save, "saving");
    try {
      const value = el.text.value;
      await patchBlock(el.id, { text_edited: value.trim() ? value : "" });
      setSaveState(el.save, "saved", stamp());
      dirtyRows.delete(el.id);
      return true;
    } catch (err) {
      el.dirty = true;
      setSaveState(el.save, "error");
      dirtyRows.add(el.id);
      if (!silent) toast("Не вдалося зберегти блок " + el.id + ": " + err.message, "err");
      return false;
    }
  }

  function markDirty(el) {
    el.dirty = true;
    dirtyRows.add(el.id);
    setSaveState(el.save, "dirty");
    if (el.dirty.timer) clearTimeout(el.dirty.timer);
    const delay = Number(CONFIG.autosaveMs) || 1500;
    el.dirty.timer = setTimeout(() => saveRow(el, { silent: false }), delay);
  }

  function saveAllRows() {
    const tasks = [];
    document.querySelectorAll("[data-block-row]").forEach((row) => {
      const el = rowElements(row);
      if (el.dirty) tasks.push(saveRow(el, { silent: true }));
    });
    if (!tasks.length) {
      toast("Немає що зберігати", "ok");
      return Promise.resolve(true);
    }
    return Promise.all(tasks).then((results) => {
      const failed = results.filter((ok) => !ok).length;
      toast(failed ? "Не збережено блоків: " + failed : "Збережено", failed ? "err" : "ok");
      return failed === 0;
    });
  }

  // ── Прев'ю одного блоку ───────────────────────────────────────────────
  async function previewRow(el) {
    await saveRow(el, { silent: false });          // прев'ю має відповідати збереженому
    const slot = el.previewSlot;
    if (!slot) return;
    slot.innerHTML = '<div class="tiny muted">Синтезую… перший раз довше: шлюз завантажує модель.</div>';
    window.htmx.ajax("POST", "/ui/blocks/" + el.id + "/preview", {
      target: slot,
      swap: "innerHTML",
      values: { voice_id: CONFIG.voiceId, engine_id: CONFIG.engineId },
    });
  }

  // ── Ініціалізація рядка ───────────────────────────────────────────────
  function initRow(row) {
    const el = rowElements(row);
    el.dirty = false;
    el.id = Number(row.dataset.blockId);

    refreshProsody(el);

    if (el.text) {
      el.text.addEventListener("input", () => markDirty(el));
      el.text.addEventListener("blur", () => saveRow(el, { silent: false }));
    }

    if (el.emotion) {
      el.emotion.addEventListener("change", async () => {
        refreshProsody(el);
        try {
          await patchBlock(el.id, { emotion: el.emotion.value });
          setSaveState(el.save, "saved", stamp());
        } catch (err) {
          setSaveState(el.save, "error");
          toast("Не вдалося змінити емоцію: " + err.message, "err");
        }
      });
    }

    if (el.intensity) {
      let timer = null;
      el.intensity.addEventListener("input", () => {
        refreshProsody(el);                        // миттєвий відгук у UI
        if (timer) clearTimeout(timer);
        timer = setTimeout(async () => {
          try {
            await patchBlock(el.id, { intensity: Number(el.intensity.value) });
            setSaveState(el.save, "saved", stamp());
          } catch (err) {
            setSaveState(el.save, "error");
          }
        }, 300);
      });
    }

    if (el.speak) {
      el.speak.addEventListener("change", async () => {
        row.classList.toggle("blk-off", !el.speak.checked);
        try {
          await patchBlock(el.id, { speak: el.speak.checked });
          setSaveState(el.save, "saved", stamp());
        } catch (err) {
          setSaveState(el.save, "error");
          toast("Не вдалося змінити озвучення: " + err.message, "err");
        }
      });
    }

    if (el.revert) {
      el.revert.addEventListener("click", async () => {
        const normalized = el.normalized ? el.normalized.textContent : "";
        try {
          await patchBlock(el.id, { text_edited: "" });
          el.text.value = normalized;
          el.dirty = false;
          dirtyRows.delete(el.id);
          setSaveState(el.save, "saved", "↺ Повернуто до нормалізованого");
        } catch (err) {
          setSaveState(el.save, "error");
          toast("Не вдалося повернути текст: " + err.message, "err");
        }
      });
    }

    if (el.preview) {
      el.preview.addEventListener("click", () => previewRow(el));
    }

    if (el.pick) {
      el.pick.addEventListener("change", () => {
        document.dispatchEvent(new CustomEvent("vs:pick", {
          detail: { id: el.id, checked: el.pick.checked },
        }));
      });
    }
  }

  function initAllRows(root) {
    (root || document).querySelectorAll("[data-block-row]").forEach(initRow);
  }

  // ── Гарячі клавіші ────────────────────────────────────────────────────
  let focusedRow = null;

  function focusRow(row) {
    if (!row) return;
    if (focusedRow) focusedRow.classList.remove("focused");
    focusedRow = row;
    row.classList.add("focused");
    row.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function rowList() {
    return Array.from(document.querySelectorAll("[data-block-row]"));
  }

  function moveFocus(step) {
    const rows = rowList();
    if (!rows.length) return;
    const index = focusedRow ? rows.indexOf(focusedRow) : -1;
    const next = rows[Math.min(rows.length - 1, Math.max(0, index + step))];
    focusRow(next || rows[0]);
  }

  function isTyping(target) {
    if (!target) return false;
    const tag = target.tagName;
    return tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT" ||
      target.isContentEditable;
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      document.dispatchEvent(new CustomEvent("vs:clear-selection"));
      return;
    }

    const meta = event.ctrlKey || event.metaKey;
    if (meta && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveAllRows();
      return;
    }

    // Решта клавіш не мають заважати набору тексту
    if (isTyping(event.target)) return;

    if (event.key === "j" || event.key === "J") { event.preventDefault(); moveFocus(1); }
    else if (event.key === "k" || event.key === "K") { event.preventDefault(); moveFocus(-1); }
    else if (event.key === " " && focusedRow) {
      event.preventDefault();
      previewRow(rowElements(focusedRow));
    } else if ((event.key === "e" || event.key === "E") && focusedRow) {
      const select = focusedRow.querySelector("[data-emotion-select]");
      if (select) { event.preventDefault(); select.focus(); }
    } else if ((event.key === "s" || event.key === "S") && focusedRow) {
      const box = focusedRow.querySelector("[data-speak]");
      if (box) {
        event.preventDefault();
        box.checked = !box.checked;
        box.dispatchEvent(new Event("change"));
      }
    }
  });

  // ── Alpine: локальний стан редактора ──────────────────────────────────
  document.addEventListener("alpine:init", () => {
    window.Alpine.data("editorState", () => ({
      selected: [],
      bulkEmotion: "neutral",
      busy: false,

      init() {
        document.addEventListener("vs:pick", (event) => {
          const { id, checked } = event.detail;
          if (checked && !this.selected.includes(id)) {
            this.selected.push(id);
          } else if (!checked) {
            this.selected = this.selected.filter((value) => value !== id);
          }
        });
        document.addEventListener("vs:clear-selection", () => this.clearSelection());
      },

      clearSelection() {
        this.selected = [];
        document.querySelectorAll("[data-pick]").forEach((box) => { box.checked = false; });
      },

      async bulkPatch(ids, payloadFor) {
        if (!ids.length) return false;
        this.busy = true;
        const updates = ids.map((id) => Object.assign({ block_id: id }, payloadFor));
        try {
          const response = await fetch("/api/v1/documents/" + CONFIG.docId + "/blocks", {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ updates }),
          });
          if (!response.ok) throw new Error("HTTP " + response.status);
          return true;
        } catch (err) {
          toast("Масова дія не вдалася: " + err.message, "err");
          return false;
        } finally {
          this.busy = false;
        }
      },

      async applyBulkEmotion() {
        // Знімок вибору беремо ДО запиту: clearSelection() його спорожнить
        const ids = this.selected.slice();
        const emotion = this.bulkEmotion;
        const ok = await this.bulkPatch(ids, { emotion });
        if (!ok) return;
        ids.forEach((id) => {
          const row = document.querySelector('[data-block-id="' + id + '"]');
          if (!row) return;
          const select = row.querySelector("[data-emotion-select]");
          if (select) select.value = emotion;
          refreshProsody(rowElements(row));
        });
        toast("Емоція застосована до блоків: " + ids.length, "ok");
        this.clearSelection();
      },

      async setBulkSpeak(enabled) {
        const ids = this.selected.slice();
        const ok = await this.bulkPatch(ids, { speak: enabled });
        if (!ok) return;
        ids.forEach((id) => {
          const row = document.querySelector('[data-block-id="' + id + '"]');
          if (!row) return;
          const box = row.querySelector("[data-speak]");
          if (box) box.checked = enabled;
          row.classList.toggle("blk-off", !enabled);
        });
        toast((enabled ? "Озвучення увімкнено: " : "Озвучення вимкнено: ") + ids.length, "ok");
        this.clearSelection();
      },
    }));
  });

  // ── Старт ─────────────────────────────────────────────────────────────
  function boot() {
    initAllRows(document);

    // htmx довантажує нові блоки — ініціалізуємо їх після вставки
    document.body.addEventListener("htmx:afterSwap", (event) => {
      initAllRows(event.target);
      window.Alpine && window.Alpine.initTree(event.target);
    });

    // Попередження про завеликий для прев'ю блок — ДО натискання
    document.querySelectorAll("[data-block-row]").forEach((row) => {
      const el = rowElements(row);
      if (!el.text || !CONFIG.maxChars) return;
      const hint = () => {
        const tooLong = el.text.value.length > CONFIG.maxChars;
        const marker = row.querySelector("[data-length-hint]");
        if (!tooLong) { if (marker) marker.remove(); return; }
        if (marker) { marker.textContent = lengthHint(el.text.value.length); return; }
        const note = document.createElement("div");
        note.className = "tiny warn-text";
        note.dataset.lengthHint = "1";
        note.textContent = lengthHint(el.text.value.length);
        el.previewSlot ? el.previewSlot.before(note) : null;
      };
      el.text.addEventListener("input", hint);
      hint();
    });

    // Попередження при спробі закрити сторінку з незбереженим
    window.addEventListener("beforeunload", (event) => {
      if (dirtyRows.size === 0) return;
      event.preventDefault();
      event.returnValue = "";
    });

    // Голос у конфігу може змінюватися через селект у панелі — тримаємо синхронно
    const voiceSelect = document.querySelector("[data-voice-select]");
    if (voiceSelect) {
      CONFIG.voiceId = voiceSelect.value;
      voiceSelect.addEventListener("change", () => { CONFIG.voiceId = voiceSelect.value; });
    }
  }

  function lengthHint(length) {
    const over = length - CONFIG.maxChars;
    return "⚠ Блок " + length + " символів: у прев'ю долетить " + CONFIG.maxChars +
      " (на " + over + " менше). Повний синтез розібʼє його на сегменти й озвучить увесь.";
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // Експорт для тестів і налагодження
  window.VSEditor = { prosody, prosodyText, saveAllRows, initAllRows };
})();
