"""The open-work list (roadmap X85; specs/15). Stdlib only at module level (g47); every consumer in an
every-session file imports this package LAZILY, inside the function that needs it, and only when
``open_work_list`` is ON. With the flag OFF nothing here is imported by the harness."""
