"""dango/web/routes/metabase_recovery.py

Browser-side recovery for the Metabase proxy: the script that notices Metabase
going down and coming back, and the helper that injects it into proxied HTML.
"""

RECOVERY_SCRIPT_PATH = "/metabase/_dango/recovery.js"
RECOVERY_SCRIPT_TAG = f'<script defer src="{RECOVERY_SCRIPT_PATH}"></script>'

# Plain JavaScript, served as an external same-origin file so Metabase's CSP cannot block it.
# Health is polled through the proxy.  Two consecutive failures mark Metabase as down; the first
# successful sample afterwards triggers exactly one reload.  Polling is never faster than five
# seconds, backs off while down, and is suspended while the tab is hidden.
RECOVERY_SCRIPT = """\
(function () {
  "use strict";
  if (window.__dangoMetabaseRecovery) { return; }
  window.__dangoMetabaseRecovery = true;

  var HEALTH_URL = "/metabase/api/health";
  var FAILURES_BEFORE_DOWN = 2;
  var VISIBLE_INTERVAL_MS = 6000;
  var DOWN_INTERVAL_MS = 5000;
  var MAX_DOWN_INTERVAL_MS = 30000;
  var REQUEST_TIMEOUT_MS = 4000;

  var script = document.currentScript;
  var down = !!(script && script.getAttribute("data-initial-state") === "down");
  var failures = down ? FAILURES_BEFORE_DOWN : 0;
  var delay = DOWN_INTERVAL_MS;
  var reloaded = false;
  var inFlight = false;
  var timer = null;

  function setStatus(text) {
    var el = document.getElementById("dango-status");
    if (el) { el.textContent = text; }
  }

  function sample() {
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var abortTimer = controller ? setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS) : null;
    var options = { cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" } };
    if (controller) { options.signal = controller.signal; }
    return fetch(HEALTH_URL, options).then(
      function (response) { return response.status === 200; },
      function () { return false; }
    ).then(function (ok) {
      if (abortTimer) { clearTimeout(abortTimer); }
      return ok;
    });
  }

  function schedule(ms) {
    if (timer) { clearTimeout(timer); }
    timer = reloaded ? null : setTimeout(tick, ms);
  }

  function handle(ok) {
    if (ok) {
      if (down) {
        reloaded = true;
        setStatus("Metabase is back. Reloading\\u2026");
        window.location.reload();
        return;
      }
      failures = 0;
      schedule(VISIBLE_INTERVAL_MS);
      return;
    }
    failures += 1;
    if (failures >= FAILURES_BEFORE_DOWN) {
      down = true;
      setStatus("Still waiting for Metabase\\u2026");
      schedule(delay);
      delay = Math.min(delay * 2, MAX_DOWN_INTERVAL_MS);
    } else {
      schedule(DOWN_INTERVAL_MS);
    }
  }

  function tick() {
    timer = null;
    if (reloaded || inFlight) { return; }
    if (document.hidden) { return; }
    inFlight = true;
    sample().then(function (ok) {
      inFlight = false;
      handle(ok);
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && !timer && !reloaded) { tick(); }
  });

  var retry = document.getElementById("dango-retry");
  if (retry) {
    retry.addEventListener("click", function () {
      if (timer) { clearTimeout(timer); timer = null; }
      setStatus("Checking Metabase\\u2026");
      tick();
    });
  }

  schedule(down ? DOWN_INTERVAL_MS : VISIBLE_INTERVAL_MS);
})();
"""


def inject_recovery_script(body: bytes) -> bytes:
    """Insert the recovery ``<script>`` before the last ``</body>``; leave *body* alone otherwise."""
    if RECOVERY_SCRIPT_PATH.encode() in body:
        return body
    index = body.lower().rfind(b"</body>")
    if index == -1:
        return body
    return body[:index] + RECOVERY_SCRIPT_TAG.encode() + body[index:]
