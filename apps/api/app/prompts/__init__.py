"""What the app actually says to a model, made readable without being rewritten.

The tutor's first request for this app was to SEE the prompts. `registry.py` is
the one place that answers it — and the only thing in here, deliberately: this
package holds no prompt text of its own and never will. It points at the
definitions where they already live (`agent/prompts.py`, `curriculum/corpus.py`,
`brain/ocr.py`, `i18n.py`, …) and renders them with the same builders the live
call path uses.

The moment a prompt string is copied in here, the viewer starts lying — it shows
the copy while the model gets the original, and nothing ever tells you.
"""
