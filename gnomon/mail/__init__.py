"""V1: the agent mail tool (flag ``agent_mail``, default OFF).

Per-agent addresses by Gmail plus-addressing on the operator's own account; the agent reads ONLY mail carrying
its own label (``agents/<instance>``), drafts replies in its own name (a Gmail DRAFT, never a send), and
may deliver a note to the operator by INSERTING it into the operator's own mailbox (``mail_to_operator``; nothing leaves
the mailbox). The OAuth token lives only in the Gnomon SERVER (DATA root, 0600); the harness verbs reach
it through the loopback ``/api/mail/verb`` route.

Modules: ``interface`` (the ONLY backend surface the verbs use: no send method exists on it),
``gmail_http`` (the stdlib HTTPS implementation, with a hard (method, path) allow-list), ``oauth``
(installed-app flow, PKCE, exact-scope assertion), ``tokenstore`` / ``config`` / ``activity`` (state under
``<DATA>/mail/``), ``service`` (label scoping, allow-list, signature, threading, logging), ``runtime``
(what the web server and the self-verb broker share).

stdlib only (google-api-python-client is NOT installed on this machine and is not needed).
"""

FLAG_NAME = "agent_mail"
VERB_NAMES = ("mail_list", "mail_read", "mail_draft", "mail_to_operator")
