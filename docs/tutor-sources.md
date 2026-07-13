# The tutor's own sources

The material the app is meant to teach from. **This file exists because these URLs were previously
recorded nowhere** — they were mentioned once in a conversation, never ingested, and the library
quietly ran on a seed script's synthetic filler for days. Anything the tutor considers "his material"
belongs here.

## The book

- `Getting Great Guitar Sounds.pdf` (repo root) — 77-page raster scan, no text layer.
  OCR'd via `LLMProvider.vision()` (Plan 9). Live: 194,671 chars, page-addressable, citable.

## The 8 course URLs (given by Chris, 2026-07-13)

```
https://proaudioexp.com/products/ultimate-guitar-tone-school
https://truefire.com/techniques-guitar-lessons/guitar-effects-survival-guide/guitar-effects-survival-guide-introduction/v13776
https://rhettshullguitarcourses.com/p/the-tone-course
https://www.licklibrary.com/learn/courses/ultimate-guitar-effects-pedals
https://course.guitargearfinder.com/
https://www.pickupmusic.com/master-classes/guitar-tone
https://truefire.com/courses/techniques-guitar-lessons/kings-of-tone/c176
https://online.berklee.edu/courses/getting-your-guitar-sound
```

**Expect these to be thin.** They are commercial course landing pages: the *actual* lessons sit behind
a paywall/login. What we can legitimately ingest is the public page — syllabus, module list, marketing
description. That is genuinely useful (it is a real-world curriculum spine written by professionals)
but it is **not** the course content itself, and the app must not pretend otherwise. A page that yields
nothing must land as `empty`, in red, with a Retry — never as a green lie (spec D6).

## Not his material — do not treat as such

- **"Guitar Tone & Gear — Course Spine"** — synthetic filler written by the Plan 7 seed script. It has
  been sitting in the library looking like his content. Delete once real sources are in.
- The 3 Wikipedia sources — 0 chars (Wikimedia TLS-fingerprints and blocks httpx). Plan 12 Task 1
  replaces the fetch client; if they still fail, they stay honestly `empty`.
