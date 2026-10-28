# Security

Gnomon runs hooks inside every agent session it launches and includes a deny guard that refuses a small set of dangerous
or protected commands and edits. The guard filters accidents and honest mistakes; it is not a security boundary against a
hostile local process, and its known gaps are stated in the documentation.

If you find a security problem, please report it privately to security@burnettzone.com and do not open a public issue. Include what you
ran, what you expected and what happened. Please give a reasonable time to respond before any public disclosure.
