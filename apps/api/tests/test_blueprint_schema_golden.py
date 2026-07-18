"""Golden test for the lesson blueprint (Plan C, Task 1).

THE ONE INVARIANT THIS UNIT MUST NEVER BREAK: `build_lesson_schema(default_blueprint())`
must deep-equal the lesson schema the engine sends today, byte-for-byte — every
`description` string, every nested `required` list order, every `additionalProperties`.

`GOLDEN_LESSON_SCHEMA` below is a FROZEN copy of `app/curriculum/depth.py::LESSON_DRAFT_SCHEMA`
as of 2026-07-18, captured programmatically (NOT re-typed) with:

    python -c "import json; from app.curriculum.depth import LESSON_DRAFT_SCHEMA as S; \\
               print(json.dumps(S, ensure_ascii=False, indent=4))"

and the JSON keyword `false` swapped to Python `False`. If depth.py's schema ever
legitimately changes, this literal changes in the SAME commit and the reviewer sees
exactly what moved. A reworded description that silently changes what the model is
sent is the one defect this unit must never ship — this is the backstop.
"""
from app.curriculum.blueprint import build_lesson_schema, default_blueprint

GOLDEN_LESSON_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string"
        },
        "summary": {
            "type": "string",
            "description": "Two sentences the tutor can read at a glance before the lesson."
        },
        "warm_up": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "5 minutes of playing to open the session. Concrete: what to play, at what tempo, why it prepares this lesson's material."
                },
                "citations": {
                    "type": "array",
                    "description": "Every page of the tutor's library this section actually drew on. source_id is the id attribute of the <source> element (e.g. \"S1\"); page is the [p.N] marker the text you used sat under. Cite ONLY pages you actually read in the library block. An empty array is the correct, honest answer for a section written from general knowledge.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {
                                "type": "string"
                            },
                            "page": {
                                "type": "integer"
                            }
                        },
                        "required": [
                            "source_id",
                            "page"
                        ],
                        "additionalProperties": False
                    }
                }
            },
            "required": [
                "body",
                "citations"
            ],
            "additionalProperties": False
        },
        "theory": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "The concept, taught. Full prose the tutor can read aloud or teach from — not bullet points, not a summary of a lesson."
                },
                "citations": {
                    "type": "array",
                    "description": "Every page of the tutor's library this section actually drew on. source_id is the id attribute of the <source> element (e.g. \"S1\"); page is the [p.N] marker the text you used sat under. Cite ONLY pages you actually read in the library block. An empty array is the correct, honest answer for a section written from general knowledge.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {
                                "type": "string"
                            },
                            "page": {
                                "type": "integer"
                            }
                        },
                        "required": [
                            "source_id",
                            "page"
                        ],
                        "additionalProperties": False
                    }
                }
            },
            "required": [
                "body",
                "citations"
            ],
            "additionalProperties": False
        },
        "demonstration": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "What the tutor plays, step by step, and what the student should be listening for. Name the fret positions, the chords, the tempo."
                },
                "citations": {
                    "type": "array",
                    "description": "Every page of the tutor's library this section actually drew on. source_id is the id attribute of the <source> element (e.g. \"S1\"); page is the [p.N] marker the text you used sat under. Cite ONLY pages you actually read in the library block. An empty array is the correct, honest answer for a section written from general knowledge.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {
                                "type": "string"
                            },
                            "page": {
                                "type": "integer"
                            }
                        },
                        "required": [
                            "source_id",
                            "page"
                        ],
                        "additionalProperties": False
                    }
                }
            },
            "required": [
                "body",
                "citations"
            ],
            "additionalProperties": False
        },
        "exercises": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "Prose introducing and sequencing the exercises."
                },
                "items": {
                    "type": "array",
                    "description": "Each exercise the student actually plays.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string"
                            },
                            "instructions": {
                                "type": "string"
                            },
                            "est_minutes": {
                                "type": "integer"
                            }
                        },
                        "required": [
                            "title",
                            "instructions",
                            "est_minutes"
                        ],
                        "additionalProperties": False
                    }
                },
                "citations": {
                    "type": "array",
                    "description": "Every page of the tutor's library this section actually drew on. source_id is the id attribute of the <source> element (e.g. \"S1\"); page is the [p.N] marker the text you used sat under. Cite ONLY pages you actually read in the library block. An empty array is the correct, honest answer for a section written from general knowledge.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {
                                "type": "string"
                            },
                            "page": {
                                "type": "integer"
                            }
                        },
                        "required": [
                            "source_id",
                            "page"
                        ],
                        "additionalProperties": False
                    }
                }
            },
            "required": [
                "body",
                "items",
                "citations"
            ],
            "additionalProperties": False
        },
        "common_mistakes": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "What students actually get wrong here, how it sounds when they do, and the correction the tutor gives."
                },
                "citations": {
                    "type": "array",
                    "description": "Every page of the tutor's library this section actually drew on. source_id is the id attribute of the <source> element (e.g. \"S1\"); page is the [p.N] marker the text you used sat under. Cite ONLY pages you actually read in the library block. An empty array is the correct, honest answer for a section written from general knowledge.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {
                                "type": "string"
                            },
                            "page": {
                                "type": "integer"
                            }
                        },
                        "required": [
                            "source_id",
                            "page"
                        ],
                        "additionalProperties": False
                    }
                }
            },
            "required": [
                "body",
                "citations"
            ],
            "additionalProperties": False
        },
        "recap": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "What was covered, in the words the student will remember."
                },
                "citations": {
                    "type": "array",
                    "description": "Every page of the tutor's library this section actually drew on. source_id is the id attribute of the <source> element (e.g. \"S1\"); page is the [p.N] marker the text you used sat under. Cite ONLY pages you actually read in the library block. An empty array is the correct, honest answer for a section written from general knowledge.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {
                                "type": "string"
                            },
                            "page": {
                                "type": "integer"
                            }
                        },
                        "required": [
                            "source_id",
                            "page"
                        ],
                        "additionalProperties": False
                    }
                }
            },
            "required": [
                "body",
                "citations"
            ],
            "additionalProperties": False
        },
        "homework": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "What to practise before the next session, for how long, and how the student knows they have it right."
                },
                "citations": {
                    "type": "array",
                    "description": "Every page of the tutor's library this section actually drew on. source_id is the id attribute of the <source> element (e.g. \"S1\"); page is the [p.N] marker the text you used sat under. Cite ONLY pages you actually read in the library block. An empty array is the correct, honest answer for a section written from general knowledge.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {
                                "type": "string"
                            },
                            "page": {
                                "type": "integer"
                            }
                        },
                        "required": [
                            "source_id",
                            "page"
                        ],
                        "additionalProperties": False
                    }
                }
            },
            "required": [
                "body",
                "citations"
            ],
            "additionalProperties": False
        },
        "qa_prompts": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "How to open the 10-minute discussion block."
                },
                "items": {
                    "type": "array",
                    "description": "Questions to put to the student, EACH WITH AN ANSWER KEY — the tutor is holding this page while the student answers.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string"
                            },
                            "answer_key": {
                                "type": "string"
                            }
                        },
                        "required": [
                            "question",
                            "answer_key"
                        ],
                        "additionalProperties": False
                    }
                },
                "citations": {
                    "type": "array",
                    "description": "Every page of the tutor's library this section actually drew on. source_id is the id attribute of the <source> element (e.g. \"S1\"); page is the [p.N] marker the text you used sat under. Cite ONLY pages you actually read in the library block. An empty array is the correct, honest answer for a section written from general knowledge.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {
                                "type": "string"
                            },
                            "page": {
                                "type": "integer"
                            }
                        },
                        "required": [
                            "source_id",
                            "page"
                        ],
                        "additionalProperties": False
                    }
                }
            },
            "required": [
                "body",
                "items",
                "citations"
            ],
            "additionalProperties": False
        }
    },
    "required": [
        "title",
        "summary",
        "warm_up",
        "theory",
        "demonstration",
        "exercises",
        "common_mistakes",
        "recap",
        "homework",
        "qa_prompts"
    ],
    "additionalProperties": False
}


def test_build_lesson_schema_of_default_equals_todays_schema():
    """The whole point of Task 1: the default blueprint reproduces today's schema."""
    assert build_lesson_schema(default_blueprint()) == GOLDEN_LESSON_SCHEMA


def test_property_order_matches_todays_schema():
    """Property order is load-bearing — it is what the model is actually sent, and a
    dict equality check (above) does not see key order. This pins it to the byte."""
    schema = build_lesson_schema(default_blueprint())
    assert list(schema["properties"].keys()) == [
        "title", "summary", "warm_up", "theory", "demonstration", "exercises",
        "common_mistakes", "recap", "homework", "qa_prompts",
    ]


def test_default_blueprint_is_deep_copied_each_call():
    """`default_blueprint()` must hand back an independent object every time — a
    shared mutable default would let one course's edit bleed into another's."""
    a = default_blueprint()
    b = default_blueprint()
    a["sections"][0]["weight"] = 999
    assert b["sections"][0]["weight"] != 999


def test_required_lists_are_exact():
    """Nested `required` order is load-bearing (it is a JSON array, order-sensitive)."""
    schema = build_lesson_schema(default_blueprint())
    assert schema["required"] == [
        "title", "summary", "warm_up", "theory", "demonstration",
        "exercises", "common_mistakes", "recap", "homework", "qa_prompts",
    ]
    assert schema["properties"]["exercises"]["required"] == ["body", "items", "citations"]
    assert schema["properties"]["qa_prompts"]["required"] == ["body", "items", "citations"]
    assert schema["properties"]["theory"]["required"] == ["body", "citations"]
