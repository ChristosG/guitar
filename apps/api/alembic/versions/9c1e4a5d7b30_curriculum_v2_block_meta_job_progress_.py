"""curriculum v2: block.meta, job.progress, student.goals, interview v2

Revision ID: 9c1e4a5d7b30
Revises: a3c7e1b90d42

FOUR SCHEMA CHANGES AND TWO DATA MIGRATIONS. The data migrations are the
interesting half — both of them are "the code changed shape and the tutor's live
rows are still the old shape", and both would surface as a 500 on his next click.

1. `block.meta`. Everything ABOUT a block that isn't the block: provenance,
   grounding tier, draft lifecycle, word counts, prev_body for Undo. Until now all
   of that lived in `target_profile`, which therefore meant two unrelated things at
   once ("who is this template for" AND "where did this content come from") — which
   is the reason `block_to_tree` serialized neither and the citation chips could not
   be rendered.

   DATA MIGRATION: the provenance already written into `target_profile.provenance`
   (by every curriculum this app has generated) is COPIED into `meta.provenance`,
   and the gap flags with it. Nothing is deleted from `target_profile` — a
   destructive rewrite of the tutor's live rows to tidy a column is not worth the
   risk, and a stale key hurts nobody.

2. `generation_job.progress`. Free-form job progress for the one job that has any.

3. `student.goals`. What the student actually wants, in the tutor's words — and it
   reaches the LESSON DRAFT prompt, which the student had never touched.

4. `curriculum_interview`: `brief`/`gap_policy`/`outline`/`root_id` in, `domain` and
   `preview` OUT.

   DATA MIGRATION, AND THIS IS THE ONE THAT WOULD HAVE 500'd: the interview's step
   list changed, and `preview` is gone from it. `answer_interview` does
   `STEP_ORDER.index(interview.step)` — so an interview row still sitting on
   `step='preview'` (the tutor opened it before the deploy, went to lunch, came
   back and clicked) raises ValueError, and he gets a 500 on a curriculum he was
   halfway through. Every persisted 'preview' becomes 'outline'; every row past it
   is left alone.
"""
from alembic import op
import sqlalchemy as sa

revision = "9c1e4a5d7b30"
down_revision = "a3c7e1b90d42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("block", sa.Column("meta", sa.JSON(), nullable=True))
    op.add_column("generation_job", sa.Column("progress", sa.JSON(), nullable=True))
    op.add_column("student", sa.Column("goals", sa.Text(), nullable=True))

    op.add_column("curriculum_interview", sa.Column("brief", sa.Text(), nullable=True))
    op.add_column(
        "curriculum_interview",
        sa.Column("gap_policy", sa.String(length=20), nullable=False,
                  server_default="general_knowledge"),
    )
    op.add_column("curriculum_interview", sa.Column("outline", sa.JSON(), nullable=True))
    op.add_column(
        "curriculum_interview", sa.Column("root_id", sa.Uuid(as_uuid=True), nullable=True)
    )

    # --- data: provenance/gap out of target_profile, into meta ---------------
    #
    # `jsonb_build_object` via a cast, because the columns are plain `json` (not
    # `jsonb`) and `json` has no `->` -building operators. Only rows that actually
    # carry provenance or a gap flag are touched.
    op.execute(
        """
        UPDATE block
        SET meta = (
            jsonb_strip_nulls(
                jsonb_build_object(
                    'provenance', (target_profile::jsonb) -> 'provenance',
                    'gap',        (target_profile::jsonb) -> 'gap',
                    'tier',       CASE
                        WHEN (target_profile::jsonb) ? 'provenance' THEN '"library"'::jsonb
                        WHEN (target_profile::jsonb) ? 'general_knowledge'
                            THEN '"general_knowledge"'::jsonb
                        WHEN (target_profile::jsonb) ? 'gap' THEN '"gap"'::jsonb
                        ELSE NULL
                    END
                )
            )
        )::json
        WHERE target_profile IS NOT NULL
          AND (
              (target_profile::jsonb) ? 'provenance'
              OR (target_profile::jsonb) ? 'gap'
          )
        """
    )

    # --- data: the step that no longer exists --------------------------------
    op.execute("UPDATE curriculum_interview SET step = 'outline' WHERE step = 'preview'")

    op.drop_column("curriculum_interview", "preview")
    op.drop_column("curriculum_interview", "domain")


def downgrade() -> None:
    op.add_column(
        "curriculum_interview", sa.Column("domain", sa.String(length=30), nullable=True)
    )
    op.add_column("curriculum_interview", sa.Column("preview", sa.JSON(), nullable=True))
    # The reverse of the step migration. 'outline' is not perfectly invertible — a
    # row that reached 'outline' legitimately after this deploy is indistinguishable
    # from one migrated up from 'preview' — so it maps back to the nearest step the
    # old machine had, which is exactly 'preview'. Named rather than hidden.
    op.execute("UPDATE curriculum_interview SET step = 'preview' WHERE step = 'outline'")
    op.drop_column("curriculum_interview", "root_id")
    op.drop_column("curriculum_interview", "outline")
    op.drop_column("curriculum_interview", "gap_policy")
    op.drop_column("curriculum_interview", "brief")

    op.drop_column("student", "goals")
    op.drop_column("generation_job", "progress")
    # `meta` is dropped, and the provenance it holds was COPIED (never moved) out of
    # target_profile — which still has it. So this loses nothing that existed before
    # the upgrade, only what Stage 6 wrote after it.
    op.drop_column("block", "meta")
