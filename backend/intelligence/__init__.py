"""Typed Jev questions and response validation; see _docs/jev-questions.md.

This package is Phase 0 product code: it turns supplied schema-1 facts into one
narrow question per contract dimension, and it validates one model response into
a schema-1 JevJudgment. It never touches audio, local paths or the network. The
bounded transport and adapter belong to backend/intelligence/jev.py (#14), which
this package deliberately does not import.
"""
