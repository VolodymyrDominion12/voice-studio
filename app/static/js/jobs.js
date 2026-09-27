/* Voice Studio — прогрес завдань (фаза F2).
 *
 * Що тут важливо і що легко пропустити (docs/FRONTEND.md, розділ 12):
 *   1) SSE віддає лише МАЙБУТНІ події — підписка створюється в момент запиту,
 *      Last-Event-ID не підтримується. Тому спершу GET-знімок, потім стрім.
 *   2) Сервер шле `: keepalive` кожні 30 с — це НЕ подія, його треба ігнорувати.
 *   3) Після завершення стрім закривається штатно: «зʼєднання втрачено» показувати
 *      не можна.
 *   4) Вкладка у фоні може призупинити стрім → на visibilitychange беремо знімок.
 *   5) Якщо SSE не піднявся — переходимо на опитування, бо прогрес без прогресу
 *      гірший за жоден.
 */

"use strict";

(function () {
  const POLL_MS = 3000;
  const MAX_SSE_FAILURES = 3;

  function el(root, selector) {
    return root.querySelector(selector);
  }

  function setText(node, text) {
    if (node) node.textContent = text;
  }

  function statusChip(status) {
    return {
      queued: ["у черзі", "queued"],
      running: ["виконується", "run"],
      done: ["готово", "ok"],
      failed: ["збій", "err"],
      cancelled: ["скасовано", "warn"],
    }[status] || [status, "queued"];
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    const total = Math.round(seconds);
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    if (minutes === 0) return rest + " с";
    return minutes + ":" + String(rest).padStart(2, "0");
  }

  function formatEta(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "";
    if (seconds < 60) return "залишилось <1 хв";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return "залишилось ~" + minutes + " хв";
    return "залишилось ~" + (minutes / 60).toFixed(1).replace(".", ",") + " год";
  }

  // ── Один трекер на завдання ───────────────────────────────────────────
  function JobTracker(root, jobId) {
    this.root = root;
    this.jobId = jobId;
    this.source = null;
    this.pollTimer = null;
    this.failures = 0;
    this.finished = false;
    this.rateWindow = [];   // [timestamp, done] — для ETA
    this.lastDone = 0;
    this.total = 0;
  }

  JobTracker.prototype.start = function () {
    this.snapshot().then(() => {
      if (!this.finished) this.openStream();
    });
  };

  JobTracker.prototype.snapshot = function () {
    return fetch("/api/v1/jobs/" + this.jobId, { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((job) => {
        if (job) this.render(job);
        return job;
      })
      .catch(() => null);
  };

  JobTracker.prototype.openStream = function () {
    if (this.finished || typeof window.EventSource === "undefined") {
      this.startPolling();
      return;
    }

    try {
      this.source = new EventSource("/api/v1/jobs/" + this.jobId + "/events");
    } catch (err) {
      this.startPolling();
      return;
    }

    this.source.addEventListener("progress", (event) => {
      this.failures = 0;
      const data = JSON.parse(event.data);
      this.render({
        id: this.jobId,
        status: data.status,
        progress: (data.percent || 0) / 100,
        done: data.done,
        total: data.total,
      });
      this.updateEta(data.done, data.total);
    });

    this.source.addEventListener("segment", () => {
      this.refreshSegments();
    });

    ["finished", "cancelled", "error"].forEach((name) => {
      this.source.addEventListener(name, (event) => {
        const data = JSON.parse(event.data || "{}");
        this.finish(data.status || name);
      });
    });

    this.source.onerror = () => {
      if (this.finished) return;
      this.failures += 1;
      if (this.failures >= MAX_SSE_FAILURES) {
        // Стрім не тримається: далі тільки опитування, інакше прогресу немає
        this.closeStream();
        this.startPolling();
      }
    };
  };

  JobTracker.prototype.closeStream = function () {
    if (this.source) {
      this.source.close();
      this.source = null;
    }
  };

  JobTracker.prototype.startPolling = function () {
    if (this.pollTimer || this.finished) return;
    this.pollTimer = setInterval(() => {
      this.snapshot().then((job) => {
        if (job && ["done", "failed", "cancelled"].includes(job.status)) {
          this.finish(job.status);
        } else {
          this.refreshSegments();
        }
      });
    }, POLL_MS);
  };

  JobTracker.prototype.stopPolling = function () {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  };

  JobTracker.prototype.updateEta = function (done, total) {
    this.total = total || this.total;
    const now = Date.now();
    this.rateWindow.push([now, done]);
    while (this.rateWindow.length > 6) this.rateWindow.shift();

    if (this.rateWindow.length < 2 || !this.total) return;
    const [t0, d0] = this.rateWindow[0];
    const [t1, d1] = this.rateWindow[this.rateWindow.length - 1];
    const elapsed = (t1 - t0) / 1000;
    const produced = d1 - d0;
    if (elapsed <= 0 || produced <= 0) return;

    const perSegment = elapsed / produced;
    const remaining = (this.total - d1) * perSegment;

    const detail = el(this.root, "[data-job-detail]");
    if (detail) {
      detail.textContent = d1 + " з " + this.total + " сегментів · " + formatEta(remaining);
    }
    this.lastDone = d1;
  };

  JobTracker.prototype.render = function (job) {
    const percent = Math.round((job.progress || 0) * 1000) / 10;
    const bar = el(this.root, "[data-job-bar]");
    if (bar) bar.style.width = percent + "%";

    const percentNode = el(this.root, "[data-job-percent]");
    setText(percentNode, String(percent).replace(".", ",") + " %");

    const chipNode = el(this.root, "[data-job-chip]");
    if (chipNode) {
      const [label, kind] = statusChip(job.status);
      chipNode.textContent = label;
      chipNode.className = "chip " + kind;
    }

    this.root.dataset.jobStatus = job.status;
    this.root.dataset.jobProgress = job.progress || 0;

    const progressbar = el(this.root, '[role="progressbar"]');
    if (progressbar) progressbar.setAttribute("aria-valuenow", String(Math.round(percent)));

    if (["done", "failed", "cancelled"].includes(job.status)) this.finish(job.status);
  };

  JobTracker.prototype.refreshSegments = function () {
    const host = el(this.root, "[data-segments-host]") ||
      document.querySelector("[data-segments-host]");
    if (!host || !window.htmx) return;
    window.htmx.ajax("GET", "/ui/jobs/" + this.jobId + "/segments", {
      target: host,
      swap: "innerHTML",
    }).then(() => {
      const count = document.querySelector("[data-segment-count]");
      const rows = host.querySelectorAll("[data-segment]");
      if (count) count.textContent = String(rows.length);
    });
  };

  JobTracker.prototype.finish = function (status) {
    if (this.finished) return;
    this.finished = true;
    this.closeStream();
    this.stopPolling();

    // Фінальний стан беремо із сервера: подія може не містити всіх полів
    this.snapshot().then(() => {
      const chipNode = el(this.root, "[data-job-chip]");
      if (chipNode && status) {
        const [label, kind] = statusChip(status);
        chipNode.textContent = label;
        chipNode.className = "chip " + kind;
      }
      this.refreshSegments();

      // Кнопки дій залежать від статусу — перезавантажуємо сторінку один раз,
      // щоб не дублювати в JS усю логіку шаблону.
      if (status === "done" || status === "failed" || status === "cancelled") {
        setTimeout(() => window.location.reload(), 1200);
      }
    });
  };

  // ── Старт ─────────────────────────────────────────────────────────────
  function boot() {
    const trackers = [];

    document.querySelectorAll("[data-job-card]").forEach((node) => {
      const id = Number(node.dataset.jobCard);
      const status = node.dataset.jobStatus;
      if (["queued", "running"].includes(status)) {
        const tracker = new JobTracker(node, id);
        tracker.start();
        trackers.push(tracker);
      }
    });

    const page = document.querySelector("[data-job-page]");
    if (page) {
      const tracker = new JobTracker(page, Number(page.dataset.jobPage));
      if (["queued", "running"].includes(page.dataset.jobStatus)) {
        tracker.start();
      } else {
        tracker.refreshSegments();
      }
      trackers.push(tracker);
    }

    // Вкладка повернулась — беремо свіжий знімок, не сподіваючись на «доганяючі» події
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      trackers.forEach((tracker) => {
        if (!tracker.finished) tracker.snapshot();
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.VSJobs = { formatEta, formatDuration, statusChip };
})();
