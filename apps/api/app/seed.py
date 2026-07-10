"""Idempotent demo-content seed (Plan 7 Task 1): makes the live cockpit
demo-ready by populating the Brain, two flagship curricula, a handful of
showcase artifacts, and one sample student — by calling the SAME engines the
HTTP API uses (`create_source`/`generate_curriculum`/`generate_artifact`/
`clone_content_subtree`), in-process, synchronously.

REPRESENTATIVE CONTENT ONLY. The tutor's own material is currently blocked
(his scanned book needs OCR; his 8 reference course links are paywalled — see
the project ledger). What's seeded instead: the design spec's (`docs/
superpowers/specs/2026-07-06-guitar-tutor-copilot-design.md` §2.2) 12-module
"tone-course spine" written out as real Brain text, three freely-ingestible
Wikipedia pages on the same domain, a beginner "Zero to Hero" track, and a
set of showcase artifacts. None of this is the tutor's proprietary IP — it
exists purely so the cockpit has something real to show before his material
arrives.

Idempotency: every "create" step below first queries for an existing row
with the same, exact, hardcoded title (a `KnowledgeSource`/`Block`/`Artifact`
title, or a `Student` name) and skips if found — so re-running `seed(db)`
against an already-seeded database is a no-op (safe to call on every
deploy). Showcase artifacts are the one place this needs a deliberate
twist: `generate_artifact` derives a persisted title from whatever the LLM
puts in the spec (`derive_title` prefers `spec["name"]`), which is NOT
guaranteed to come back byte-identical across runs/providers — so
`_ensure_artifact` forces the artifact's title to our own hardcoded, stable
string right after generating it, and keys its own idempotency check on that
same string. This is a seed-script-only choice (it does not change
`generate_artifact`'s behavior for any other caller); it trades a little bit
of "whatever the model wants to call it" for a demo gallery with predictable,
re-run-safe titles.

`fresh=True` first deletes: (a) this script's own previously-seeded rows, by
the same hardcoded titles/name used for idempotency above, and (b) a fixed
list of throwaway curricula titles accumulated on the live `guitar` DB from
earlier manual testing during development (e.g. "Stack Health Check") — so
that a fresh demo reseed starts from a genuinely clean slate rather than
accumulating cruft next to it. Deleting a course Block tree also deletes any
Artifact rows attached to a Block inside it: `Artifact.block_id` is
`ON DELETE SET NULL` (an artifact is allowed to outlive the lesson it was
attached to — see `app.models.artifact`), which is the right default for
normal use but would silently leave orphaned artifacts behind here, so this
module deletes them explicitly before deleting the tree. `Assignment` rows
need no such handling: `Assignment.curriculum_block_id` is `ON DELETE
CASCADE`, so deleting a course root Block already removes any Assignment
that points at it.

Robustness: every individual create step (one source, one curriculum, one
artifact, the student+assignment pair) is wrapped in its own try/except — a
single bad URL ingest or LLM hiccup logs and moves on rather than aborting
the whole run, so a demo reseed still gets "mostly there" instead of "all or
nothing." `db.rollback()` in each except clause mirrors `ingest_source`'s/
`run_curriculum_job`'s own recovery pattern: it clears whatever the failed
step may have left half-flushed, so it can't poison the later steps sharing
this same caller-owned session.

Run directly against the live `guitar` DB (NOT `guitar_test` — this uses
`app.db.SessionLocal`, which binds to `settings.database_url`) via, from
`apps/api` with the venv active:
    python -m app.seed              # skips anything already seeded
    python -m app.seed --fresh      # wipes prior seeded/demo rows first

The unit test (`tests/test_seed.py`) stubs `generate_curriculum`/
`generate_artifact`/`ingest_source` with fast fakes — no live LLM/embed call
there. The live run (Plan 7 Task 2) uses the real provider and is slow
(minutes): 2 curricula + several artifacts, each a guided-JSON call.
"""
import argparse
import logging

from sqlalchemy import delete, select

from app.artifacts.generate import generate_artifact
from app.curriculum.assign import clone_content_subtree
from app.curriculum.generate import generate_curriculum
from app.db import SessionLocal
from app.models.artifact import Artifact
from app.models.block import Block
from app.models.curriculum import Assignment
from app.models.knowledge import KnowledgeSource
from app.models.student import Student
from app.routers.knowledge import create_source
from app.schemas.knowledge import SourceCreate

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Content: the design-spec §2.2 "tone-course spine", expanded module-by-module
# into real (if representative, not proprietary) paragraphs. Order/headings
# match the spec table exactly for traceability back to it.
# ---------------------------------------------------------------------------

_TONE_SOURCE_TITLE = "Guitar Tone & Gear — Course Spine"

_TONE_MODULES: list[tuple[str, str]] = [
    (
        "What is tone",
        "Tone is the overall character of a guitar's sound: the blend of harmonics, "
        "attack, sustain, and distortion that makes one instrument sound bright and "
        "glassy and another sound thick and saturated. Tone is shaped by an unbroken "
        "chain — fingers, guitar, cable, effects, amplifier, speaker, room — and every "
        "link colors the signal before it reaches a listener's ear. Harmonics (the "
        "overtone series above the fundamental note) determine whether a tone sounds "
        "warm, nasal, or piercing; distortion adds new harmonics on top of that by "
        "clipping the waveform, which is why a driven amp sounds thicker than a clean "
        "one. But the single biggest variable is the player: pick attack, finger "
        "vibrato, palm muting, and dynamics change the sound more than almost any "
        "pedal or amp setting, which is the origin of the well-worn saying that tone "
        "is in the hands. Two players plugged into the identical rig will sound "
        "different, and the same player will sound different from one night to the "
        "next depending on touch alone.",
    ),
    (
        "The Guitar",
        "The guitar itself is the first link in the tone chain. Body type and wood "
        "affect resonance and sustain — a solid-body guitar (alder, ash, mahogany) "
        "sustains differently than a semi-hollow or hollow-body instrument, though "
        "pickups and amplification dominate an electric guitar's sound far more than "
        "wood does for an acoustic. Pickup type is the single biggest guitar-side "
        "tone decision: single-coil pickups (Stratocaster/Telecaster-style) are "
        "bright, articulate, and slightly thinner, with an audible 60-cycle hum; "
        "humbuckers pair two coils wound in opposite polarity to cancel that hum, "
        "producing a thicker, warmer, higher-output sound suited to overdrive and "
        "distortion. Volume and tone controls, pickup-selector switches, and "
        "coil-splitting options let a player blend or roll off pickups on the fly. "
        "Scale length (the distance from nut to bridge) affects string tension and "
        "feel — a longer scale (Fender-style, 25.5in) feels snappier and brighter "
        "than a shorter scale (Gibson-style, 24.75in) at the same string gauge. Setup "
        "— action height, intonation, neck relief, string gauge — is the free tone "
        "upgrade most players skip: a well-set-up guitar plays in tune up the neck "
        "and lets a player's dynamics come through cleanly.",
    ),
    (
        "Amps",
        "The amplifier is usually the biggest single tone-shaping stage in the "
        "chain, especially a tube amp. A tube amp has several gain stages: the "
        "preamp shapes the input signal and, when driven hard, produces a smoother, "
        "more harmonically rich distortion; the tone stack (bass/mid/treble "
        "controls, wired differently amp to amp) sculpts the frequency response "
        "before the signal reaches the power section; the power amp and its output "
        "transformer add their own compression and power sag as they're pushed "
        "toward clipping. Gain staging is the art of deciding where in that chain "
        "distortion happens — preamp gain, master volume, or the whole amp run hot "
        "— and it changes the character of the breakup as much as the amount of it. "
        "Amp families each have a signature voice: Fender amps (Twin, Deluxe Reverb) "
        "are clean-headroom, scooped-mid, sparkly platforms that respond well to "
        "pedals in front of them; Marshall amps (Plexi, JCM800) push midrange and "
        "break up earlier into a classic rock crunch; Vox amps (AC30) have a chimey "
        "top end and a distinctive class-A power section; modern high-gain amps "
        "(Mesa/Boogie, EVH, 5150-style) are voiced for saturated, tight, "
        "cascading-gain distortion built for rock and metal rhythm tones.",
    ),
    (
        "Speakers & Cabs",
        "A speaker cabinet is not a passive afterthought — it shapes tone as much "
        "as the amp head driving it. Impedance (measured in ohms) must be matched "
        "between amp and cabinet, since a mismatch can starve the power section of "
        "load or, worse, damage a tube amp's output transformer; multiple speakers "
        "can be wired in series, parallel, or series-parallel to hit a target "
        "impedance while changing how the speakers share the signal. Speaker size "
        "and type (a 12in Celestion Vintage 30 vs a vintage-voiced Greenback, a 10in "
        "Fender-style speaker, or a 4x12 cabinet vs a 1x12 combo) change breakup "
        "character, low-end tightness, and how big a rig feels in a room. For "
        "recording, microphone choice and placement on the cabinet matter "
        "enormously: on-axis (aimed at the center of the cone) sounds brighter and "
        "more aggressive, off-axis or edge-of-cone placement sounds warmer and "
        "rounder, and moving a mic a few centimeters can change a tone more than "
        "swapping an amp. Close-mic'ing versus adding a room mic further trades "
        "directness for the sense of space and cabinet 'breathing' a listener hears "
        "live.",
    ),
    (
        "Signal flow",
        "Signal flow is the order effects sit in on their way from guitar to amp, "
        "and it matters because each pedal reacts to whatever came before it. The "
        "conventional order — tuner, then compression, then gain (overdrive/"
        "distortion/fuzz), then modulation, then time-based effects (delay, reverb) "
        "last — exists because putting gain after modulation or delay tends to "
        "sound muddy or fizzy, while gain first keeps pitch tracking and note "
        "definition clean going into the coloring effects. Buffers matter on longer "
        "boards: a buffer restores a guitar's high-end signal strength after it's "
        "traveled through several feet of cable and multiple bypassed pedals, since "
        "a guitar's passive pickup signal degrades ('tone-sucks') over long cable "
        "runs and through true-bypass switches wired in series. True bypass means a "
        "pedal, when off, is completely out of the circuit (no coloration, but no "
        "buffering either); buffered bypass keeps a buffer active at all times. An "
        "effects (FX) loop on an amp lets time-based and modulation effects sit "
        "after the preamp's gain stage but before the power amp, which usually "
        "sounds clearer than putting a delay or reverb in front of an already-"
        "distorted amp input.",
    ),
    (
        "Gain fx",
        "Gain effects are the pedals that add distortion, saturation, or dynamic "
        "control before the amp (or in its effects loop). A compressor evens out "
        "picking dynamics — it turns down loud notes and/or turns up quiet ones — "
        "which is prized for clean funk/country picking and for sustaining single "
        "notes without changing their tone much. Overdrive pedals emulate a tube "
        "amp being pushed into soft clipping: they add warmth and harmonics while "
        "staying touch-responsive and cleaning up when the guitar's volume is "
        "rolled back. Distortion pedals clip harder and more symmetrically, "
        "producing a more saturated, compressed, and less dynamic sound suited to "
        "rock rhythm tones. Fuzz pedals clip the hardest and most asymmetrically of "
        "all, producing a thick, sometimes gated or splatty texture associated with "
        "1960s psychedelic and garage tones. Stacking — running two gain pedals in "
        "series, typically a lower-gain overdrive into a higher-gain drive, or a "
        "clean boost into a distortion — is a common technique to build a more "
        "complex, layered gain texture than any single pedal produces alone, since "
        "each stage's clipping character interacts with the one after it.",
    ),
    (
        "Modulation",
        "Modulation effects use a low-frequency oscillator (LFO) to continuously "
        "vary some property of the signal over time, adding movement and width to "
        "an otherwise static tone. Chorus splits the signal, detunes and delays the "
        "copy slightly, and blends it back with the dry signal to simulate multiple "
        "instruments playing slightly out of sync — a shimmering, thickening effect "
        "popular on clean tones. A flanger mixes the dry signal with a very short, "
        "continuously-swept delayed copy, creating a distinctive sweeping 'jet "
        "plane' comb-filter sound. A phaser splits the signal through a series of "
        "all-pass filters that shift phase at a swept rate, producing a swirling "
        "effect that's subtler and less metallic than a flanger. Tremolo "
        "rhythmically varies the volume of the signal (not the pitch), producing a "
        "pulsing on/off effect that ranges from a gentle wobble to a choppy "
        "stutter, while true pitch vibrato rhythmically bends the pitch up and down "
        "instead. Rotary (Leslie-speaker-style) effects combine amplitude and pitch "
        "modulation from a physically rotating speaker horn and drum, producing a "
        "complex swirling Doppler effect associated with vintage organ and some "
        "guitar tones.",
    ),
    (
        "Time fx",
        "Time-based effects repeat or extend the signal to add space and depth. "
        "Delay records the signal and plays it back after a set time, and the "
        "character of that repeat changes with the setting: a short slapback delay "
        "(under roughly 120ms) thickens a note without an obvious distinct echo, "
        "associated with 1950s rockabilly; a dotted-eighth delay syncs the repeat to "
        "create a rhythmic, cascading pattern widely used in melodic rock and "
        "ambient lead playing; a ping-pong delay alternates repeats between the "
        "left and right stereo channels for a wide, spacious effect. Reverb "
        "simulates the reflections of a physical space. Spring reverb (built into "
        "many vintage amps) has a distinctive 'boingy,' slightly metallic character "
        "from bouncing a signal down a physical spring; plate reverb simulates the "
        "dense, smooth decay of a vibrating metal plate, often used in studio "
        "recording; larger algorithmic 'ambient' or hall reverbs simulate bigger "
        "rooms or fully synthetic spaces for atmospheric, sustained textures. Delay "
        "and reverb are usually placed last in the signal chain (or in an amp's "
        "effects loop) so earlier gain and modulation stages shape a clean, "
        "well-defined signal before it's smeared across time.",
    ),
    (
        "Other",
        "Several effect categories fall outside the gain/modulation/time buckets "
        "but are core to a versatile rig. A wah pedal is a swept bandpass filter "
        "controlled by a rocking foot treadle, boosting a narrow band of "
        "frequencies that sweeps up and down as the pedal rocks, producing the "
        "vocal 'wah' associated with funk and classic rock lead playing. EQ pedals "
        "(graphic or parametric) let a player boost or cut specific frequency bands "
        "to correct a guitar or amp's tonal weaknesses, or to carve out a solo "
        "boost that cuts through a mix. Pitch effects — octave pedals, "
        "harmonizers, and pitch-shifters — add a note above or below the played "
        "pitch, or shift the whole signal to a different key, for thickening or "
        "special effects. Volume swells, achieved with a volume pedal or the "
        "guitar's own volume knob, fade a note in after it's picked (rather than at "
        "the moment of the pick attack), producing an ambient, bowed-string-like "
        "quality popular in ambient and pedal-steel-influenced playing.",
    ),
    (
        "Rigs",
        "A 'rig' is the complete, curated signal chain a player assembles and "
        "depends on for their whole sound, and rig design is its own skill "
        "separate from picking good individual gear. Pedalboard design is about "
        "physical and electrical layout: power-supply isolation (to avoid "
        "ground-loop hum across many pedals), signal order, and cable routing to "
        "keep the board reliable on a dark stage. 'Wet/dry/wet' rigs split the "
        "signal into a dry (unaffected) center amp and two 'wet' amps carrying only "
        "the time/modulation effects panned left and right, producing a huge "
        "stereo image live without smearing the dry tone. The 'four-cable method' "
        "integrates a multi-channel amp's front-of-amp input and effects loop with "
        "a pedalboard or effects processor simultaneously: gain-stage pedals "
        "(drive, distortion) run into the amp's normal input, while modulation and "
        "time-based effects run in the amp's effects loop, so every effect sits in "
        "the position — before or after the preamp's distortion — where it sounds "
        "best, a setup especially common with modern high-gain amps and "
        "multi-effects units.",
    ),
    (
        "Iconic tones by era",
        "Studying iconic guitarists' tones is one of the fastest ways to "
        "internalize how gear choices translate to sound. Jimi Hendrix (late "
        "1960s) paired a Fender Stratocaster's bright single-coils with a cranked "
        "Marshall stack and a Fuzz Face, plus a wah pedal — a template for "
        "expressive, harmonically rich fuzz-driven lead tone. Eric Clapton's late-"
        "1960s 'woman tone' (Cream era) rolled the guitar's tone knob nearly all "
        "the way off on a humbucker-equipped guitar into an overdriven amp, "
        "producing a thick, dark, vocal sustain. Stevie Ray Vaughan combined a "
        "Stratocaster (heavy strings, low action) with a stack of overdrive pedals "
        "into a loud tweed-style Fender amp for an aggressive, dynamic blues-rock "
        "tone. David Gilmour's Pink Floyd-era tone leaned on a Stratocaster, a Big "
        "Muff-style fuzz, and extensive delay to create sustained, soaring, "
        "atmospheric leads. The Edge (U2) built a signature sound almost entirely "
        "from dotted-eighth delay repeats layered over clean-to-mildly-driven amp "
        "tones, turning a simple chord into a cascading, chiming rhythmic pattern. "
        "Eddie Van Halen's 'brown sound' combined a modified high-gain amp with a "
        "flanger, producing a saturated but articulate tone built for fast, "
        "dynamic rock lead and rhythm playing.",
    ),
    (
        "Context",
        "The same gear can sound very different depending on context, and "
        "adapting to that context is itself a tone skill. Live tone must cut "
        "through a full band mix and translate through an unfamiliar PA and room, "
        "so players often scoop less midrange live than they would want in "
        "isolation, since a mid-scooped guitar can disappear in a live mix; studio "
        "tone, heard in isolation or against a produced mix, can afford to be more "
        "extreme or idiosyncratic because a mix engineer will place and EQ it "
        "deliberately. Genre shapes go-to EQ recipes: blues and classic rock tones "
        "tend to favor present midrange for a vocal, cutting lead tone; metal "
        "tones typically scoop mids and emphasize tight low end and a present top "
        "end for clarity under heavy distortion; funk and country clean tones "
        "favor bright top end and controlled bass for percussive articulation. Amp "
        "modeling and profiling technology (digital amp/cab/mic emulations) has "
        "matured enough to reproduce most of the tone-shaping stages described in "
        "this course entirely in software, letting a player carry dozens of 'amps' "
        "in a single unit for silent practice, recording, or low-volume live use — "
        "though many players still prefer a real tube amp and cabinet for the way "
        "it interacts physically with a room and a picking hand.",
    ),
]

TONE_SOURCE_TEXT = "\n\n".join(
    f"Module {i}: {heading}\n{paragraph}" for i, (heading, paragraph) in enumerate(_TONE_MODULES)
)

_WIKI_SOURCES: list[tuple[str, str]] = [
    ("Humbucker (Wikipedia)", "https://en.wikipedia.org/wiki/Humbucker"),
    ("Distortion (music) (Wikipedia)", "https://en.wikipedia.org/wiki/Distortion_(music)"),
    ("Guitar amplifier (Wikipedia)", "https://en.wikipedia.org/wiki/Guitar_amplifier"),
]

_TONE_CURRICULUM_TITLE = "Guitar Tone & Gear"
_BEGINNER_CURRICULUM_TITLE = "Zero to Hero — Beginner Guitar"

_CHORD_ARTIFACTS: list[tuple[str, str]] = [
    ("G major open", "G major open chord diagram for guitar, standard tuning, open position"),
    ("E minor open", "E minor open chord diagram for guitar, standard tuning, open position"),
    ("C major open", "C major open chord diagram for guitar, standard tuning, open position"),
    ("D major open", "D major open chord diagram for guitar, standard tuning, open position"),
]

_STUDENT_NAME = "Maria Ioannou"

# Throwaway curricula titles accumulated on the live `guitar` DB from earlier
# manual/live-verification testing during development (not created by this
# script) — swept on `fresh=True` alongside this script's own seed titles so
# a reseed starts from a genuinely clean slate. See module docstring.
_LEGACY_DEMO_COURSE_TITLES = {
    "Blues Rhythm Basics",
    "API Contract Check",
    "Open Chords E2E Browser Check",
    "Stack Health Check",
    "Guitar Tone Basics",
}


# ---------------------------------------------------------------------------
# Brain sources
# ---------------------------------------------------------------------------

def _ensure_source(
    db, summary: dict, *, kind: str, title: str,
    domain: str | None = None, language: str | None = None,
    text: str | None = None, url: str | None = None,
) -> None:
    existing = db.scalars(select(KnowledgeSource).where(KnowledgeSource.title == title)).first()
    if existing is not None:
        log.info("seed: knowledge source %r already exists, skipping", title)
        summary["skipped"].append(f"knowledge_source: {title!r} (already exists)")
        return
    try:
        payload = SourceCreate(kind=kind, title=title, domain=domain, language=language, text=text, url=url)
        source_out = create_source(payload, db)
        log.info("seed: created knowledge source %r (status=%s)", title, source_out.status)
        summary["created"].setdefault("knowledge_sources", []).append(title)
    except Exception:
        db.rollback()
        log.exception("seed: failed to create/ingest knowledge source %r", title)
        summary["skipped"].append(f"knowledge_source: {title!r} (failed, see log)")


def _seed_knowledge_sources(db, summary: dict) -> None:
    _ensure_source(
        db, summary, kind="text", title=_TONE_SOURCE_TITLE,
        text=TONE_SOURCE_TEXT, domain="tone", language="en",
    )
    for title, url in _WIKI_SOURCES:
        _ensure_source(db, summary, kind="url", title=title, url=url, domain="tone", language="en")


# ---------------------------------------------------------------------------
# Curricula
# ---------------------------------------------------------------------------

def _ensure_curriculum(
    db, summary: dict, *, title: str, language: str, profile: dict,
    domain: str | None = None, target_minutes_total: int | None = None,
) -> None:
    existing = db.scalars(select(Block).where(Block.kind == "course", Block.title == title)).first()
    if existing is not None:
        log.info("seed: curriculum %r already exists, skipping", title)
        summary["skipped"].append(f"curriculum: {title!r} (already exists)")
        return
    try:
        root_id = generate_curriculum(
            db, title=title, language=language, profile=profile,
            domain=domain, target_minutes_total=target_minutes_total,
        )
        log.info("seed: generated curriculum %r (root_id=%s)", title, root_id)
        summary["created"].setdefault("curricula", []).append(title)
    except Exception:
        db.rollback()
        log.exception("seed: failed to generate curriculum %r", title)
        summary["skipped"].append(f"curriculum: {title!r} (failed, see log)")


def _seed_curricula(db, summary: dict) -> None:
    _ensure_curriculum(
        db, summary, title=_TONE_CURRICULUM_TITLE, language="en",
        profile={"level": "intermediate"}, domain="tone", target_minutes_total=1500,
    )
    _ensure_curriculum(
        db, summary, title=_BEGINNER_CURRICULUM_TITLE, language="en",
        profile={"level": "beginner"}, target_minutes_total=1200,
    )


# ---------------------------------------------------------------------------
# Showcase artifacts
# ---------------------------------------------------------------------------

def _ensure_artifact(db, summary: dict, *, kind: str, title: str, prompt: str, ground: bool = False) -> None:
    existing = db.scalars(
        select(Artifact).where(Artifact.kind == kind, Artifact.title == title)
    ).first()
    if existing is not None:
        log.info("seed: artifact %s/%r already exists, skipping", kind, title)
        summary["skipped"].append(f"artifact: {kind}/{title!r} (already exists)")
        return
    try:
        artifact = generate_artifact(db, kind=kind, prompt=prompt, ground=ground)
        # Pin the title to our own stable string rather than trusting
        # generate_artifact's LLM-derived one — see module docstring.
        artifact.title = title
        db.commit()
        log.info("seed: generated artifact %s/%r", kind, title)
        summary["created"].setdefault("artifacts", []).append(title)
    except Exception:
        db.rollback()
        log.exception("seed: failed to generate artifact %s/%r", kind, title)
        summary["skipped"].append(f"artifact: {kind}/{title!r} (failed, see log)")


def _seed_artifacts(db, summary: dict) -> None:
    for title, prompt in _CHORD_ARTIFACTS:
        _ensure_artifact(db, summary, kind="chord_diagram", title=title, prompt=prompt)

    _ensure_artifact(
        db, summary, kind="scale_diagram", title="A minor pentatonic",
        prompt="A minor pentatonic scale diagram for guitar, one octave, root position",
    )
    _ensure_artifact(
        db, summary, kind="tone_recipe", title="Stevie Ray Vaughan Texas Flood",
        prompt=(
            "Stevie Ray Vaughan's 'Texas Flood' tone: guitar, amp, drive pedal, "
            "signal chain, and playing-hand technique"
        ),
        ground=True,
    )
    _ensure_artifact(
        db, summary, kind="signal_chain", title="Classic pedalboard order",
        prompt=(
            "A classic pedalboard signal chain from guitar to amp: tuner, "
            "compressor, overdrive/distortion, modulation, delay, reverb"
        ),
    )
    _ensure_artifact(
        db, summary, kind="amp_settings", title="Fender clean",
        prompt=(
            "Fender-style clean amp dial settings (gain, bass, mid, treble, "
            "reverb, volume) for a bright clean tone"
        ),
    )


# ---------------------------------------------------------------------------
# Student + assignment
# ---------------------------------------------------------------------------

def _seed_student_and_assignment(db, summary: dict) -> None:
    """Create `_STUDENT_NAME` + assign her the beginner curriculum, skipped as
    ONE unit if she already exists (per the brief). Split into two try/
    excepts (not one) so a failure is attributed to the right half: if
    student creation itself fails there is nothing to assign, but if only
    the assignment half fails the student is still correctly reported as
    created. Known accepted gap (mirrors `ingest_source`'s own "may be left
    stranded" tradeoff): if the assignment half fails, a later `seed(db)`
    re-run will see the now-existing student and skip this whole step again
    without retrying the assignment — acceptable for a demo seed script.
    """
    existing = db.scalars(select(Student).where(Student.name == _STUDENT_NAME)).first()
    if existing is not None:
        log.info("seed: student %r already exists, skipping", _STUDENT_NAME)
        summary["skipped"].append(f"student: {_STUDENT_NAME!r} (already exists)")
        return

    try:
        student = Student(name=_STUDENT_NAME, level="beginner", preferred_language="el")
        db.add(student)
        db.commit()
        log.info("seed: created student %r", _STUDENT_NAME)
        summary["created"]["student"] = _STUDENT_NAME
    except Exception:
        db.rollback()
        log.exception("seed: failed to create student %r", _STUDENT_NAME)
        summary["skipped"].append(f"student: {_STUDENT_NAME!r} (failed, see log)")
        return  # no student was created -> nothing to assign

    try:
        template_root = db.scalars(
            select(Block).where(Block.kind == "course", Block.title == _BEGINNER_CURRICULUM_TITLE)
        ).first()
        if template_root is None:
            log.warning(
                "seed: beginner curriculum %r not found, skipping assignment for %r",
                _BEGINNER_CURRICULUM_TITLE, _STUDENT_NAME,
            )
            summary["skipped"].append(
                f"assignment: {_STUDENT_NAME!r} (curriculum {_BEGINNER_CURRICULUM_TITLE!r} missing)"
            )
            return

        clone_content_subtree(db, template_root, parent_id=None, student_id=student.id)
        db.add(Assignment(student_id=student.id, curriculum_block_id=template_root.id))
        db.commit()
        log.info("seed: assigned %r to %r", _BEGINNER_CURRICULUM_TITLE, _STUDENT_NAME)
        summary["created"]["assignment"] = f"{_BEGINNER_CURRICULUM_TITLE} -> {_STUDENT_NAME}"
    except Exception:
        db.rollback()
        log.exception("seed: failed to assign %r to %r", _BEGINNER_CURRICULUM_TITLE, _STUDENT_NAME)
        summary["skipped"].append(f"assignment: {_STUDENT_NAME!r} (failed, see log)")


# ---------------------------------------------------------------------------
# fresh=True: wipe prior seeded + legacy demo-titled rows first
# ---------------------------------------------------------------------------

def _subtree_ids(db, root_id) -> list:
    """BFS: `root_id` plus every Block transitively parented under it,
    across both content and delivery planes (a full wipe, unlike
    `clone_content_subtree`'s content-only walk) — mirrors
    `test_curriculum_generate.py`'s own `_descendants` helper, plus the root.
    """
    ids = [root_id]
    frontier = [root_id]
    while frontier:
        rows = db.scalars(select(Block.id).where(Block.parent_id.in_(frontier))).all()
        ids.extend(rows)
        frontier = rows
    return ids


def _delete_course_trees(db, title: str) -> int:
    """Delete every ROOT course Block titled `title` (there can be more than
    one: a template root and any per-student clone share the same title —
    `clone_content_subtree` copies `title` verbatim), plus any Artifact
    attached anywhere inside each tree. Returns how many roots were deleted.
    """
    roots = db.scalars(
        select(Block).where(
            Block.kind == "course", Block.title == title, Block.parent_id.is_(None)
        )
    ).all()
    for root in roots:
        ids = _subtree_ids(db, root.id)
        db.execute(delete(Artifact).where(Artifact.block_id.in_(ids)))
        # ORM cascade="all, delete-orphan" + DB ON DELETE CASCADE remove the
        # rest of the tree; DB ON DELETE CASCADE on Assignment.curriculum_
        # block_id removes any Assignment pointing at this exact root.
        db.delete(root)
    if roots:
        db.commit()
    return len(roots)


def _delete_prior(db) -> None:
    course_titles = _LEGACY_DEMO_COURSE_TITLES | {_TONE_CURRICULUM_TITLE, _BEGINNER_CURRICULUM_TITLE}
    for title in sorted(course_titles):
        try:
            deleted = _delete_course_trees(db, title)
            if deleted:
                log.info("seed(fresh): deleted %d curriculum tree(s) titled %r", deleted, title)
        except Exception:
            db.rollback()
            log.exception("seed(fresh): failed to delete curriculum tree %r", title)

    showcase_titles = [t for t, _ in _CHORD_ARTIFACTS] + [
        "A minor pentatonic", "Stevie Ray Vaughan Texas Flood",
        "Classic pedalboard order", "Fender clean",
    ]
    try:
        db.execute(delete(Artifact).where(Artifact.title.in_(showcase_titles)))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("seed(fresh): failed to delete showcase artifacts")

    source_titles = [_TONE_SOURCE_TITLE, *[t for t, _ in _WIKI_SOURCES]]
    try:
        db.execute(delete(KnowledgeSource).where(KnowledgeSource.title.in_(source_titles)))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("seed(fresh): failed to delete knowledge sources")

    try:
        student = db.scalars(select(Student).where(Student.name == _STUDENT_NAME)).first()
        if student is not None:
            db.delete(student)  # cascades her Assignment row(s) + any Block.student_id clone
            db.commit()
            log.info("seed(fresh): deleted student %r", _STUDENT_NAME)
    except Exception:
        db.rollback()
        log.exception("seed(fresh): failed to delete student %r", _STUDENT_NAME)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def seed(db, *, fresh: bool = False) -> dict:
    """Seed representative demo content; safe to call repeatedly.

    Returns `{"created": {...}, "skipped": [...]}`: `created` maps each
    category ("knowledge_sources"/"curricula"/"artifacts"/"student"/
    "assignment") to what this call actually created (absent/empty if this
    call created nothing in that category); `skipped` lists every
    already-exists-or-failed item, human-readable, across all categories.

    `fresh=True` deletes prior seeded + known legacy demo-titled rows first
    (see `_delete_prior`), then proceeds through the same steps below — so a
    single `seed(db, fresh=True)` call both resets and rebuilds.
    """
    summary: dict = {"created": {}, "skipped": []}
    if fresh:
        _delete_prior(db)

    _seed_knowledge_sources(db, summary)
    _seed_curricula(db, summary)
    _seed_artifacts(db, summary)
    _seed_student_and_assignment(db, summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Seed representative demo content.")
    parser.add_argument(
        "--fresh", action="store_true",
        help="wipe prior seeded rows + known legacy demo curricula first, then reseed clean",
    )
    args = parser.parse_args()

    _db = SessionLocal()
    try:
        _summary = seed(_db, fresh=args.fresh)
    finally:
        _db.close()

    print(_summary)
