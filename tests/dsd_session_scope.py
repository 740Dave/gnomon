"""The DSD modules that are in the SESSION build scope since the DSD self-verbs (flag ``dsd_self_verbs``), by name.

The session's tool server process (``harness/self_mcp.py``, session code) imports ``gnomon.dsd.dsd_ops`` LAZILY when one of the four
verbs (send_dsd, my_dsds, withdraw_dsd, replace_dsd) is called with the flag ON. The first import of ANY ``gnomon.dsd`` submodule
runs the package ``__init__``, which eagerly imports model, persist, resolve, roundtrip, inbox, live and writer (and through them documents,
senttime and notice); ``answers_reader`` is imported when an answered DSD's answers are listed or the inline notice is built. These are
MEASURED at call time (tests/test_dsd_session_scope.py runs the call and reads ``sys.modules``) and are also what the static import scan
(``importscope``) finds. Every other ``gnomon/dsd/`` module (answer_stats, heal_answer_time, sections, submit_proof) is web-server
code and stays OUT of the session scope; the server-only web modules and ``gnomon/app/`` stay out too.

If this list must grow, the session build id changes when those files change; update the list ON PURPOSE (and the ledger sentence).
"""
SESSION_DSD_MODULES = frozenset({
    "gnomon/dsd/__init__.py", "gnomon/dsd/answers_reader.py", "gnomon/dsd/documents.py",
    "gnomon/dsd/dsd_ops.py", "gnomon/dsd/inbox.py", "gnomon/dsd/live.py", "gnomon/dsd/model.py",
    "gnomon/dsd/notice.py", "gnomon/dsd/persist.py", "gnomon/dsd/resolve.py", "gnomon/dsd/roundtrip.py",
    "gnomon/dsd/senttime.py", "gnomon/dsd/writer.py"})
