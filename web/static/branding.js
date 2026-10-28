/* Header logo as DEPLOYMENT DATA (round 5 item 8). The neutral tree ships no asset: the header holds an empty span. When the deployment
 * carries <deploy_root>/defaults/branding.json, the web job serves that one file at GET /api/branding/logo and this script swaps the span
 * for the image once it has loaded. A 404 (no logo) or any error leaves the empty span exactly as it was. */
(function () {
  "use strict";
  var slot = document.getElementById("gm-logo-slot");
  if (!slot) { return; }
  var img = new Image();
  img.className = "gm-logo";
  img.alt = "";
  img.onload = function () { if (slot.parentNode) { slot.parentNode.replaceChild(img, slot); } };
  img.onerror = function () { /* no deployment logo: keep the empty span */ };
  img.src = "/api/branding/logo";
}());
