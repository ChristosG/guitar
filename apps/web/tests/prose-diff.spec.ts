import { test, expect } from "@playwright/test";
import { diffProse, fold, similarity, type DiffBlock } from "../src/lib/prose-diff";

// Node-only: no browser, no server. `prose-diff.ts` is deliberately pure so it
// can be tested like this — the alignment is the hard part of the feature and
// it deserves tests that run in milliseconds and say exactly what broke.
//
// EVERY CASE IS GREEK, and that is not decoration. This codebase has been bitten
// three separate times by English-shaped text handling (see
// `apps/api/app/text/normalize.py`'s docstring): an English-only suite here
// would pass while the feature was broken in the tutor's default locale.

function kinds(blocks: DiffBlock[]): string[] {
  return blocks.map((b) => b.kind);
}

function added(block: DiffBlock): string {
  return block.kind === "edit"
    ? block.pieces.filter((p) => p.type === "add").map((p) => p.text).join("")
    : "";
}

function removed(block: DiffBlock): string {
  return block.kind === "edit"
    ? block.pieces.filter((p) => p.type === "del").map((p) => p.text).join("")
    : "";
}

test.describe("fold — the same rules as the API's", () => {
  test("strips accents, unifies final sigma, lowercases", () => {
    expect(fold("Τονικότητα")).toBe("τονικοτητα");
    expect(fold("μάθημα")).toBe("μαθημα");
    expect(fold("ΦΩΣ")).toBe(fold("φώς"));
  });

  test("survives empty and non-Greek input", () => {
    expect(fold("")).toBe("");
    expect(fold("Amp")).toBe("amp");
  });
});

test.describe("similarity", () => {
  test("identical prose is 1, unrelated prose is near 0", () => {
    const a = "Ο ενισχυτής χρωματίζει τον ήχο με τον προενισχυτή του.";
    expect(similarity(a, a)).toBe(1);
    expect(similarity(a, "Οι χορδές καθορίζουν το ύφος και το πάχος.")).toBeLessThan(0.2);
  });

  test("Greek function words alone do NOT make two paragraphs similar", () => {
    // The reason the metric is over BIGRAMS. Both sentences are drowning in
    // «και/το/της/με»; on unigrams they would score as related.
    const a = "Το πετάλι και ο ενισχυτής με τη ρύθμιση του gain.";
    const b = "Το βιβλίο και η καρέκλα με τη θέα της θάλασσας.";
    expect(similarity(a, b)).toBeLessThan(0.25);
  });

  test("a paragraph the AI made LONGER still matches its own original", () => {
    // Dice rather than Jaccard, precisely for this: "give more detail about the
    // amp" is the normal instruction and must not read as a low match.
    const before = "Ο ενισχυτής χρωματίζει τον ήχο.";
    const after =
      "Ο ενισχυτής χρωματίζει τον ήχο. Ο προενισχυτής προσθέτει αρμονικές, " +
      "και το τελικό στάδιο συμπιέζει τη δυναμική.";
    expect(similarity(before, after)).toBeGreaterThan(0.35);
  });

  test("a one-word heading matches itself despite having no bigrams", () => {
    expect(similarity("Ζέσταμα", "Ζέσταμα")).toBe(1);
  });
});

test.describe("diffProse", () => {
  test("an appended sentence is an EDIT, not a rewrite", () => {
    const before = "Ο ενισχυτής χρωματίζει τον ήχο.";
    const after = "Ο ενισχυτής χρωματίζει τον ήχο. Δοκίμασε χαμηλό gain.";
    const d = diffProse(before, after);

    expect(d.rewritten).toBe(false);
    expect(kinds(d.blocks)).toEqual(["edit"]);
    expect(added(d.blocks[0])).toContain("gain");
    expect(removed(d.blocks[0]).trim()).toBe("");
  });

  test("a reworded paragraph shows both sides, still as one block", () => {
    const before = "Ο ενισχυτής χρωματίζει τον ήχο της κιθάρας σημαντικά.";
    const after = "Ο ενισχυτής διαμορφώνει τον ήχο της κιθάρας σημαντικά.";
    const d = diffProse(before, after);

    expect(kinds(d.blocks)).toEqual(["edit"]);
    expect(removed(d.blocks[0])).toContain("χρωματίζει");
    expect(added(d.blocks[0])).toContain("διαμορφώνει");
  });

  test("an inserted paragraph is an ADD and leaves its neighbours alone", () => {
    const before = "Η πρώτη παράγραφος για τον ήχο.\n\nΗ τρίτη παράγραφος για τις χορδές.";
    const after =
      "Η πρώτη παράγραφος για τον ήχο.\n\n" +
      "Η δεύτερη παράγραφος για τον ενισχυτή.\n\n" +
      "Η τρίτη παράγραφος για τις χορδές.";
    expect(kinds(diffProse(before, after).blocks)).toEqual(["same", "add", "same"]);
  });

  test("a deleted paragraph is a DEL", () => {
    const before = "Η πρώτη για τον ήχο.\n\nΗ δεύτερη για τον ενισχυτή.\n\nΗ τρίτη για τις χορδές.";
    const after = "Η πρώτη για τον ήχο.\n\nΗ τρίτη για τις χορδές.";
    expect(kinds(diffProse(before, after).blocks)).toEqual(["same", "del", "same"]);
  });

  test("paragraphs never cross — order is preserved", () => {
    const before = "Άλφα για τον ήχο.\n\nΒήτα για τις χορδές.";
    const after = "Βήτα για τις χορδές.\n\nΆλφα για τον ήχο.";
    const d = diffProse(before, after);
    // A swap must NOT be reported as two clean matches in the wrong order —
    // the aligner is monotonic, so one side is an add and the other a del.
    expect(d.blocks.length).toBeGreaterThan(2);
  });

  test("accents and final sigma alone are NOT a change", () => {
    // «φώς» vs «φως» is the exact shape of a Greek false positive: a diff that
    // flagged it would cry wolf on every re-render of the same text.
    const d = diffProse("Ο ήχος και το φώς.", "Ο ηχος και το φως.");
    expect(kinds(d.blocks)).toEqual(["same"]);
  });

  test("a total rewrite SAYS SO instead of producing confetti", () => {
    const before =
      "Ο ενισχυτής χρωματίζει τον ήχο με τον προενισχυτή του και το τελικό του στάδιο.";
    const after =
      "Οι χορδές καθορίζουν το ύφος: πάχος, υλικό, ηλικία, και τρόπος περιέλιξης.";
    const d = diffProse(before, after);

    expect(d.rewritten).toBe(true);
    expect(d.similarity).toBeLessThan(0.2);
  });

  test("a mostly-kept lesson is NOT called a rewrite", () => {
    const before = "Πρώτη παράγραφος.\n\nΔεύτερη παράγραφος.\n\nΤρίτη παράγραφος.";
    const after = "Πρώτη παράγραφος.\n\nΔεύτερη παράγραφος.\n\nΤρίτη παράγραφος αλλαγμένη λίγο.";
    expect(diffProse(before, after).rewritten).toBe(false);
  });

  test("empty sides are handled and never throw", () => {
    expect(kinds(diffProse("", "Κάτι νέο.").blocks)).toEqual(["add"]);
    expect(kinds(diffProse("Κάτι παλιό.", "").blocks)).toEqual(["del"]);
    expect(diffProse("", "").blocks).toEqual([]);
  });

  test("identical input produces no noise at all", () => {
    const text = "Ο ήχος της κιθάρας.\n\nΟι χορδές και ο ενισχυτής.";
    const d = diffProse(text, text);
    expect(kinds(d.blocks)).toEqual(["same", "same"]);
    expect(d.rewritten).toBe(false);
  });
});
