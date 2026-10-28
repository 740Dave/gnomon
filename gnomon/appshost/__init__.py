"""The Gnomon Apps host (roadmap).

A manifest-driven host that installs (on the operator's confirm), supervises (one launchd user agent per app,
running the preflight wrapper ``gnomon-app-launch <id>``), starts, stops and opens tools that live OUTSIDE
Gnomon's source tree.

Round 3 (g73-amendment-4-2026-10-02, 4b): a status poll executes NOTHING (HTTP + file hashing only); every Python the
host runs is isolated (-I -B); the env an app gets is a closed allow-list with a host-owned HOME; ``app_open`` is the
7th agent verb (app id only, g73-amendment-4b).

Modules (no import-time work in any of them beyond constants, g47b):

* :mod:`.manifest`  the manifest schema registry (schema_version 1) and strict validation.
* :mod:`.state`     host state under the DATA root (<DATA>/apps/...), the single audit writer, the port table.
* :mod:`.ports`     loopback port assignment, the free-port test, and the lsof listener probes.
* :mod:`.launchd`   the INJECTED launchd adapter (the real one refuses to run under pytest, g31) + plist builder.
* :mod:`.pins`      the manifest pin and the code pin: a hash of EVERY file under the app root, no exclusions, no git
                    process (g73-amendment-4 rule 2); path values used in an argv; the venv by file identity.
* :mod:`.rules`     the filesystem half of the argv / writes-path rules, and the isolated-interpreter hardening.
* :mod:`.redact`    pattern redaction of secret-shaped strings in a log tail (rule 5).
* :mod:`.licence`   the licence helper (fingerprint + expiry ONLY).
* :mod:`.preflight` every precondition, in one place (the single enforcement point, g73).
* :mod:`.launch`    the wrapper entry point ``python -m gnomon.appshost.launch <id>``.
* :mod:`.host`      :class:`AppsHost`, the controller the hub verbs and the web routes share.
* :mod:`.verbs`     the seven hub verbs, their caller scopes, and the legacy aliases.

Flag: ``apps_tab`` (default OFF). With it OFF the hub advertises none of the new verbs, the legacy
``launch_app`` / ``terminate_app`` / ``list_running_apps`` behave exactly as before, and every Apps web route
answers 404.
"""
