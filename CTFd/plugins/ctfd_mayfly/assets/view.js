CTFd._internal.challenge.data = undefined;

// TODO: Remove in CTFd v4.0 (matches the stock challenge type's own view.js)
CTFd._internal.challenge.renderer = null;
CTFd._internal.challenge.render = null;

CTFd._internal.challenge.preRender = function () {};

/**
 * The Launch/status panel is plain DOM + fetch, injected directly into the modal rather
 * than wired into the theme's Alpine.js reactivity (x-data="Challenge") -- that component
 * belongs to CTFd core, and reaching into it would break on any theme update that changes
 * its internal shape. Polling every 2s against /plugins/ctfd_mayfly/status is simple, theme-
 * independent, and matches exactly how the orchestrator's own demo console polls its
 * lifecycle (see its components/challenge-card.tsx) -- the same state machine on both ends:
 * queued -> provisioning -> healthy -> expiring -> reaped -> failed.
 */
(function () {
  var POLL_INTERVAL_MS = 2000;
  var pollTimer = null;
  var currentRunId = null;

  function modalRoot() {
    return document.getElementById("challenge-window");
  }

  function qs(selector) {
    var root = modalRoot();
    return root ? root.querySelector(selector) : null;
  }

  function csrfHeaders() {
    return {
      "Content-Type": "application/json",
      "CSRF-Token": (window.init && window.init.csrfNonce) || "",
    };
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  /**
   * A silent failure anywhere in this file (an exception, a swallowed .catch()) has been
   * the shape of every bug in this plugin so far -- "sometimes nothing renders" with nothing
   * in the UI to say why. This surfaces it directly in the panel's own spot, visible in
   * whatever the player/tester is already looking at, instead of only in a devtools console
   * that may not be open.
   */
  function showPanelError(context, err) {
    console.error("[mayfly] " + context, err);
    var root = modalRoot();
    if (!root) return;
    var existing = root.querySelector("#mayfly-error");
    if (existing) existing.remove();
    var anchor = root.querySelector(".submit-row, #challenge-input, #challenge");
    if (!anchor) return;
    var div = document.createElement("div");
    div.id = "mayfly-error";
    div.className = "alert alert-danger text-start mb-3";
    div.textContent = "[mayfly] " + context + ": " + (err && err.message ? err.message : err);
    if (anchor.parentNode) anchor.parentNode.insertBefore(div, anchor);
    else anchor.insertBefore(div, anchor.firstChild);
  }

  function panelHtml(challengeId) {
    return (
      '<div id="mayfly-panel" class="mayfly-panel text-center mb-3" data-challenge-id="' +
      challengeId +
      '">' +
      '  <div id="mayfly-launch-row">' +
      '    <button id="mayfly-launch-btn" type="button" class="btn btn-primary">Launch</button>' +
      "  </div>" +
      '  <div id="mayfly-status-row" style="display:none;">' +
      '    <div id="mayfly-state" class="fw-bold mb-1"></div>' +
      '    <div id="mayfly-url"></div>' +
      '    <div id="mayfly-countdown" class="text-muted small"></div>' +
      '    <div id="mayfly-actions" class="mt-2" style="display:none;">' +
      '      <button id="mayfly-extend-btn" type="button" class="btn btn-sm btn-outline-secondary">Extend</button> ' +
      '      <button id="mayfly-stop-btn" type="button" class="btn btn-sm btn-outline-danger">Stop</button>' +
      "    </div>" +
      "  </div>" +
      '  <div id="mayfly-failed-row" style="display:none;">' +
      '    <div class="alert alert-danger text-start" id="mayfly-triage"></div>' +
      "  </div>" +
      "</div>"
    );
  }

  // Friendlier than the raw state string, but still one label per value in InstanceState
  // (lib/types.ts on the orchestrator side) -- no new vocabulary invented on this end.
  var STATE_LABELS = {
    queued: "Queued — waiting for a launch slot",
    provisioning: "Provisioning — booting your sandbox",
    healthy: "Deployed",
    expiring: "Expiring soon",
  };

  function renderState(data) {
    var stateEl = qs("#mayfly-state");
    if (!stateEl) return; // modal was closed mid-poll

    var errEl = qs("#mayfly-error");
    if (errEl) errEl.remove(); // a later successful render clears a transient earlier failure

    var launchRow = qs("#mayfly-launch-row");
    var statusRow = qs("#mayfly-status-row");
    var failedRow = qs("#mayfly-failed-row");
    var actionsEl = qs("#mayfly-actions");

    if (data.state === "failed") {
      launchRow.style.display = "none";
      statusRow.style.display = "none";
      failedRow.style.display = "";
      var triageEl = qs("#mayfly-triage");
      triageEl.textContent = data.triage
        ? JSON.stringify(data.triage)
        : "Instance failed to start.";
      stopPolling();
      return;
    }

    if (data.state === "reaped") {
      var launchBtn = qs("#mayfly-launch-btn");
      if (launchBtn) {
        launchBtn.disabled = false;
        launchBtn.textContent = "Launch";
      }
      launchRow.style.display = "";
      statusRow.style.display = "none";
      failedRow.style.display = "none";
      stopPolling();
      return;
    }

    launchRow.style.display = "none";
    failedRow.style.display = "none";
    statusRow.style.display = "";
    stateEl.textContent = STATE_LABELS[data.state] || data.state;

    // Extend/Stop only make sense once there's an actual live instance to act on -- queued
    // and provisioning have nothing running yet, so those buttons remain hidden until then.
    var isLive = data.state === "healthy" || data.state === "expiring";
    if (actionsEl) actionsEl.style.display = isLive ? "" : "none";

    var urlEl = qs("#mayfly-url");
    urlEl.innerHTML = data.url
      ? '<a href="' + data.url + '" target="_blank" rel="noopener noreferrer">' + data.url + "</a>"
      : "";

    var countdownEl = qs("#mayfly-countdown");
    if (data.expires_at) {
      var remainingMs = new Date(data.expires_at).getTime() - Date.now();
      countdownEl.textContent = remainingMs > 0 ? "Expires in " + formatDuration(remainingMs) : "Expiring...";
    } else {
      countdownEl.textContent = "";
    }
  }

  // A raw second count (e.g. "1437s") is unreadable at TTL scale (minutes to tens of
  // minutes) -- m/s is what a player actually wants to glance at mid-challenge.
  function formatDuration(ms) {
    var totalSeconds = Math.max(Math.floor(ms / 1000), 0);
    var minutes = Math.floor(totalSeconds / 60);
    var seconds = totalSeconds % 60;
    return minutes + "m " + (seconds < 10 ? "0" : "") + seconds + "s";
  }

  function pollStatus(runId) {
    fetch("/plugins/ctfd_mayfly/status?run_id=" + encodeURIComponent(runId), {
      credentials: "same-origin",
    })
      .then(function (res) {
        return res.json();
      })
      .then(renderState)
      .catch(function (err) {
        showPanelError("status poll failed", err);
      });
  }

  function startPolling(runId) {
    currentRunId = runId;
    stopPolling();
    pollStatus(runId);
    pollTimer = setInterval(function () {
      pollStatus(runId);
    }, POLL_INTERVAL_MS);
  }

  function launch(challengeId) {
    // Disabled immediately, not left clickable until the first status poll resolves --
    // that poll only happens after this fetch itself completes, which is enough of a gap
    // for an impatient double-click to fire a second launch (harmless, since the backend
    // is idempotent, but confusing to watch happen).
    var launchBtn = qs("#mayfly-launch-btn");
    if (launchBtn) {
      launchBtn.disabled = true;
      launchBtn.textContent = "Launching…";
    }

    fetch("/plugins/ctfd_mayfly/launch", {
      method: "POST",
      credentials: "same-origin",
      headers: csrfHeaders(),
      body: JSON.stringify({ challenge_id: challengeId }),
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data.run_id) {
          startPolling(data.run_id);
        } else if (launchBtn) {
          launchBtn.disabled = false;
          launchBtn.textContent = "Launch";
        }
      })
      .catch(function () {
        if (launchBtn) {
          launchBtn.disabled = false;
          launchBtn.textContent = "Launch";
        }
      });
  }

  /**
   * .submit-row is the intended anchor (matches the theme's own challenge.html exactly), but
   * postRender can fire before Alpine has finished painting the modal's body, and different
   * themes may not expose that exact class at all -- so this tries a short list of anchors,
   * in order. A fixed-attempt poll (10 attempts * 150ms = 1.5s) used to back this and gave up
   * silently past that window -- fine on a fast paint, but "sometimes no Launch button" on a
   * slower one (a cold Cloud Run instance still loading its own assets, a slow first paint,
   * etc.), with nothing visible to the player when it happened. A MutationObserver instead
   * reacts to the anchor actually appearing, with no arbitrary deadline short enough to race
   * a real render -- INJECT_TIMEOUT_MS is a leak guard (stop observing if the modal is closed
   * without the anchor ever appearing at all), not the thing doing the waiting.
   */
  var ANCHOR_SELECTORS = [".submit-row", "#challenge-input", "#challenge"];
  var INJECT_TIMEOUT_MS = 10000;

  function findAnchor() {
    for (var i = 0; i < ANCHOR_SELECTORS.length; i++) {
      var el = qs(ANCHOR_SELECTORS[i]);
      if (el) return { el: el, selector: ANCHOR_SELECTORS[i] };
    }
    return null;
  }

  /**
   * The theme's own modal container (#challenge-window) isn't guaranteed to be fully torn
   * down and rebuilt between two different challenges of this same type -- a leftover
   * #mayfly-panel from whichever challenge was open before this one satisfies a plain
   * existence check and blocks a fresh panel from ever being injected for the new one
   * (confirmed: this is exactly what "sometimes no Launch button" turned out to be). Tag the
   * panel with the challenge id it belongs to and compare, instead of just checking whether
   * *a* panel exists at all.
   */
  function removeStalePanel() {
    var existing = qs("#mayfly-panel");
    if (!existing) return;

    var challengeIdField = qs("#challenge-id");
    var currentChallengeId = challengeIdField ? challengeIdField.value : null;
    if (existing.getAttribute("data-challenge-id") === currentChallengeId) return; // still current

    stopPolling();
    currentRunId = null;
    existing.remove();
  }

  function insertPanel(anchor) {
    var challengeIdField = qs("#challenge-id");
    var challengeId = challengeIdField ? challengeIdField.value : "";

    console.log("[mayfly] injecting Launch panel before " + anchor.selector + " (challenge " + challengeId + ")");

    var wrapper = document.createElement("div");
    wrapper.innerHTML = panelHtml(challengeId);
    var panel = wrapper.firstChild;

    if (anchor.selector === "#challenge") {
      // Last-resort fallback: no submit-row-like element at all, so just prepend into the
      // challenge tab pane itself rather than trying to insert "before" a sibling.
      anchor.el.insertBefore(panel, anchor.el.firstChild);
    } else {
      anchor.el.parentNode.insertBefore(panel, anchor.el);
    }

    wirePanelEvents();
  }

  function currentChallengeId() {
    var field = qs("#challenge-id");
    return field ? parseInt(field.value, 10) : null;
  }

  /**
   * This file is only ever requested/executed for a mayfly-type challenge's OWN modal open —
   * CTFd loads a challenge type's `script` conditionally, per challenge. But the
   * shown.bs.modal listener below is bound to #challenge-window, the modal element shared by
   * EVERY challenge regardless of type, and it only gets replaced the next time THIS file
   * runs again (i.e. the next time a mayfly challenge is opened) -- see that listener's own
   * comment. Open a standard-type challenge right after a mayfly one and the stale listener
   * still fires, with nothing to stop it blindly injecting a Launch panel into a challenge
   * that has no orchestrator-backed instance at all (confirmed: this is exactly the reported
   * "standard challenge still has a Launch button" bug). CTFd's own theme (challenges.js)
   * stores the full challenge object -- type included -- in this Alpine store on every load,
   * independent of which type-specific script is or isn't loaded, so it's a reliable signal
   * this file can check on every modal open, not just the ones where it happens to reload.
   */
  function currentChallengeType() {
    try {
      var store = window.Alpine && Alpine.store("challenge");
      return store && store.data ? store.data.type : null;
    } catch (err) {
      return null;
    }
  }

  function injectPanel() {
    if (currentChallengeType() !== "mayfly") {
      var stale = qs("#mayfly-panel");
      if (stale) {
        stopPolling();
        currentRunId = null;
        stale.remove();
      }
      return;
    }

    removeStalePanel();
    if (qs("#mayfly-panel")) {
      // Already injected and current -- but "current" only means it belongs to this
      // challenge, not that it's showing this challenge's latest state. A panel that
      // survived from an earlier open of the SAME challenge (Alpine didn't tear
      // #challenge-window down between opens) never gets recreated, so wirePanelEvents()
      // -- and the resumeIfLive() call inside it -- never runs again on this open. Re-check
      // here unconditionally instead of only at panel-creation time, which is what made a
      // hard refresh (the one path that's guaranteed to force a fresh panel) look like the
      // fix when it was really just the only path still calling resumeIfLive() at all.
      resumeIfLive(currentChallengeId());
      return;
    }

    var immediate = findAnchor();
    if (immediate) {
      insertPanel(immediate);
      return;
    }

    var root = modalRoot() || document.body;
    var timedOut = false;
    var timeoutId = setTimeout(function () {
      timedOut = true;
      observer.disconnect();
      console.error(
        "[mayfly] gave up looking for a place to inject the Launch panel after " +
          INJECT_TIMEOUT_MS +
          "ms -- none of " +
          ANCHOR_SELECTORS.join(", ") +
          " ever appeared inside #challenge-window"
      );
    }, INJECT_TIMEOUT_MS);

    var observer = new MutationObserver(function () {
      if (timedOut) return;
      removeStalePanel();
      if (qs("#mayfly-panel")) return;
      var anchor = findAnchor();
      if (!anchor) return;
      clearTimeout(timeoutId);
      observer.disconnect();
      insertPanel(anchor);
    });
    observer.observe(root, { childList: true, subtree: true });
  }

  function wirePanelEvents() {
    var challengeId = currentChallengeId();

    qs("#mayfly-launch-btn").addEventListener("click", function () {
      launch(challengeId);
    });

    qs("#mayfly-extend-btn").addEventListener("click", function () {
      if (!currentRunId) return;
      // No feedback at all here before -- a click just silently fired the fetch, with
      // nothing to show it registered until the next 2s poll (or not at all, if it failed).
      var btn = qs("#mayfly-extend-btn");
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Extending…";
      }
      fetch("/plugins/ctfd_mayfly/extend", {
        method: "POST",
        credentials: "same-origin",
        headers: csrfHeaders(),
        body: JSON.stringify({ run_id: currentRunId }),
      })
        .then(function (res) {
          if (btn) {
            btn.disabled = false;
            btn.textContent = "Extend";
          }
          if (!res.ok) {
            return res
              .json()
              .catch(function () {
                return {};
              })
              .then(function (body) {
                showPanelError("extend failed", body.error || res.status);
              });
          }
          // Success: the very next poll (<=2s away) re-renders #mayfly-countdown with the
          // real, now-extended expires_at -- nothing more to do here than end the
          // "Extending…" state, above.
        })
        .catch(function (err) {
          if (btn) {
            btn.disabled = false;
            btn.textContent = "Extend";
          }
          showPanelError("extend failed", err);
        });
    });

    qs("#mayfly-stop-btn").addEventListener("click", function () {
      if (!currentRunId) return;
      // Calling stopPolling() right here (the previous version did) killed the poller
      // before it ever got a chance to see the state actually flip to "reaped" -- which is
      // the thing that switches the panel back to showing Launch (see renderState's
      // "reaped" branch, a few lines up). The panel just froze on the old healthy view
      // until the modal was closed and reopened (confirmed: exactly the reported bug).
      // Keep polling -- the natural transition to "reaped" stops it itself.
      var stopBtn = qs("#mayfly-stop-btn");
      if (stopBtn) {
        stopBtn.disabled = true;
        stopBtn.textContent = "Stopping…";
      }
      fetch("/plugins/ctfd_mayfly/stop", {
        method: "POST",
        credentials: "same-origin",
        headers: csrfHeaders(),
        body: JSON.stringify({ run_id: currentRunId }),
      }).catch(function (err) {
        if (stopBtn) {
          stopBtn.disabled = false;
          stopBtn.textContent = "Stop";
        }
        showPanelError("stop failed", err);
      });
    });

    resumeIfLive(challengeId);
  }

  /**
   * currentRunId only ever lived in this closure's in-memory state -- closing the modal (or
   * a hard refresh) throws that away, and the panel defaulted back to showing the Launch
   * button even though the instance itself was never actually stopped and is sitting fine
   * server-side the whole time. /current is a read-only lookup (never provisions anything,
   * unlike /launch) for exactly this: reopening a challenge with a live instance should
   * resume showing its status/URL immediately, not require clicking Launch again just to
   * rediscover it via launch()'s own idempotent return.
   */
  function resumeIfLive(challengeId) {
    // currentChallengeId() reads #challenge-id's value, which CTFd's own theme JS populates
    // asynchronously right after the modal opens. Caught before that write lands, the field
    // is still "" and parseInt gives NaN here -- sending that through would 500 on the
    // Postgres-backed API (see api.py's current(), which now rejects it too), but the real
    // fix is to just not ask yet: bail and let the next resumeIfLive call (a later modal
    // open) find the field populated.
    if (challengeId === null || isNaN(challengeId)) return;

    fetch("/plugins/ctfd_mayfly/current?challenge_id=" + encodeURIComponent(challengeId), {
      credentials: "same-origin",
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data.run_id) startPolling(data.run_id);
      })
      .catch(function (err) {
        showPanelError("resume-check failed", err);
      });
  }

  CTFd._internal.challenge.postRender = function () {
    try {
      injectPanel();
    } catch (err) {
      showPanelError("postRender threw", err);
    }
  };

  /**
   * postRender only fires when CTFd actually re-runs its challenge-load pipeline (fetch +
   * script (re)load) -- confirmed (via a real browser, not guessing) that reopening the SAME
   * challenge you just closed skips that pipeline entirely, since the id hasn't changed. But
   * the modal's content still gets wiped and reapplied via Alpine's x-html binding on every
   * open, which erases our injected panel (never part of that HTML string) with nothing left
   * to reinject it -- postRender simply never runs again to do so. #challenge-window itself
   * is the one stable, persistent element across every challenge switch (only its innerHTML
   * changes), so listening for Bootstrap's own "shown.bs.modal"/"hidden.bs.modal" events on
   * it -- which fire on every open/close regardless of whether CTFd's own pipeline ran -- are
   * hooks that don't depend on that pipeline at all.
   *
   * A first version of this guarded against re-binding with a DOM-attribute flag, on the
   * assumption "bind once" was enough. It wasn't: each genuine reload runs this whole file
   * fresh, creating a BRAND NEW closure with its own injectPanel/currentRunId/pollTimer -- but
   * the flag left the FIRST closure's listener permanently bound, since no later execution
   * ever got to add its own. Opening a second, different challenge fired that stale listener,
   * whose stale-but-still-live pollTimer got redirected onto the new challenge's run and then
   * kept ticking after that challenge's modal closed -- a zombie poller that went on
   * overwriting whatever challenge was open next with the wrong run's state/URL (confirmed:
   * this is exactly "both challenges point to the same sandbox URL"). Storing the current
   * handlers on the element itself (not a module-load flag) lets each fresh execution remove
   * the previous closure's listeners before adding its own, and hidden.bs.modal now stops
   * polling on close so a closed challenge never has a live timer left to redirect.
   */
  var challengeWindowEl = document.getElementById("challenge-window");
  if (challengeWindowEl) {
    var previousShown = challengeWindowEl.__mayflyShownHandler;
    if (previousShown) challengeWindowEl.removeEventListener("shown.bs.modal", previousShown);
    var previousHidden = challengeWindowEl.__mayflyHiddenHandler;
    if (previousHidden) challengeWindowEl.removeEventListener("hidden.bs.modal", previousHidden);

    var shownHandler = function () {
      try {
        injectPanel();
      } catch (err) {
        showPanelError("shown.bs.modal handler threw", err);
      }
    };
    var hiddenHandler = function () {
      stopPolling();
    };

    challengeWindowEl.__mayflyShownHandler = shownHandler;
    challengeWindowEl.__mayflyHiddenHandler = hiddenHandler;
    challengeWindowEl.addEventListener("shown.bs.modal", shownHandler);
    challengeWindowEl.addEventListener("hidden.bs.modal", hiddenHandler);
  }

  /**
   * The "N Solves" tab (.challenge-solves in the base challenge.html) is rendered once via
   * server-side Jinja ({{ solves }}) when the modal first opens -- it's not reactively bound
   * to anything, so it doesn't move from 0 to 1 after a correct submission until the modal
   * is closed and reopened (confirmed: this is stock CTFd behavior, not specific to this
   * challenge type -- a "standard" challenge has the exact same staleness). Bumping it here,
   * client-side, the moment a fresh "correct" comes back is a small enough fix to make
   * directly rather than waiting on CTFd core to change how that tab renders.
   */
  function bumpSolvesTab() {
    var tab = qs(".challenge-solves");
    if (!tab) return;
    var match = tab.textContent.match(/(\d+)/);
    var next = (match ? parseInt(match[1], 10) : 0) + 1;
    tab.textContent = next + (next === 1 ? " Solve" : " Solves");
  }

  CTFd._internal.challenge.submit = function (preview) {
    var challenge_id = parseInt(CTFd.lib.$("#challenge-id").val());
    var submission = CTFd.lib.$("#challenge-input").val();

    var body = {
      challenge_id: challenge_id,
      submission: submission,
    };
    var params = {};
    if (preview) {
      params["preview"] = true;
    }

    return CTFd.api.post_challenge_attempt(params, body).then(function (response) {
      if (response.status === 429) {
        return response;
      }
      if (response.status === 403) {
        return response;
      }
      // Only a fresh "correct" counts as a new solve -- "already_solved" (re-submitting
      // after already solving) was already reflected in that count on the modal's last
      // open, so bumping again there would overcount.
      if (response.data && response.data.status === "correct") {
        bumpSolvesTab();
      }
      return response;
    });
  };
})();
