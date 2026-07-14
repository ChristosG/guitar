import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the Notes cockpit page: every
// `/notes/*` call (plus the read-only `/students` list the create/edit
// form's student picker uses) is intercepted via `page.route` and answered
// from an in-memory fixture — no backend needs to be running.
//
// Route patterns are anchored to the API's own origin, NOT a bare
// `**/notes/**`-style suffix glob (`knowledge.spec.ts`'s convention): unlike
// every `/knowledge/*` path that file mocks (each has a segment after
// "knowledge"), `GET`/`POST /notes` has NO segment after "notes", so a bare
// suffix glob would also match this app's OWN same-origin page URL
// (`http://localhost:3100/en/notes` ends in "/notes" too) — exactly the
// pitfall `cockpit.spec.ts` documents for `/students`. Anchoring to
// `API_ORIGIN` (mirroring that file's `mockStudentsApi`) removes the
// ambiguity outright instead of relying on coincidence.
const API_ORIGIN = "http://localhost:8791";

// `Access-Control-Allow-Origin` must echo the app's real origin (and pair
// with `Allow-Credentials`) rather than "*": since the auth slice made every
// call in `lib/api.ts` `credentials: "include"`, a browser REJECTS a
// wildcard-ACAO response outright — the page then renders its "could not
// load" error and every assertion below it fails for a reason that has
// nothing to do with what the test is checking.
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};

interface FixtureNote {
  id: string;
  title: string;
  body: string;
  tags: string[];
  student_id: string | null;
  promoted_to_knowledge: boolean;
  created_at: string;
  updated_at: string;
}

function seedNote(overrides: Partial<FixtureNote> = {}): FixtureNote {
  const now = new Date().toISOString();
  return {
    id: randomUUID(),
    title: "Practice reminder",
    body: "Work on alternate picking for 10 minutes daily.",
    tags: [],
    student_id: null,
    promoted_to_knowledge: false,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

interface FixtureStudent {
  id: string;
  name: string;
  birthdate: string | null;
  level: string | null;
  instrument: string | null;
  preferred_language: string;
  status: string;
  created_at: string;
  updated_at: string;
}

function seedStudent(overrides: Partial<FixtureStudent> = {}): FixtureStudent {
  const now = new Date().toISOString();
  return {
    id: randomUUID(),
    name: "Maria Ioannou",
    birthdate: null,
    level: "intermediate",
    instrument: "guitar",
    preferred_language: "el",
    status: "active",
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

/** Mocks every `/notes` endpoint the page calls (list/create/update/delete/
 * promote) plus a read-only `GET /students` for the form's student picker.
 * Same shape as `cockpit.spec.ts`'s `mockStudentsApi`/`mockCurriculaApi`: one
 * handler per origin-anchored pattern, an "unexpected request -> 500"
 * catch-all so a routing mistake fails loudly instead of leaking to a real
 * backend, and mutable fixture state so a POST/PATCH/DELETE/promote is
 * genuinely reflected in the next GET (not just in the single fulfilled
 * response) — proving the page re-fetches rather than trusting its own
 * optimistic state. */
async function mockNotesApi(
  page: Page,
  { notes: initialNotes = [] as FixtureNote[], students = [] as FixtureStudent[] } = {},
) {
  const notes = [...initialNotes];
  const calls = { list: 0, create: 0, update: 0, delete: 0, promote: 0, studentsList: 0 };
  const lastBody: { create?: unknown; update?: unknown } = {};
  const unexpected: string[] = [];

  async function notesHandler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    if (pathname === "/notes" && method === "GET") {
      calls.list++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(notes),
      });
      return;
    }

    if (pathname === "/notes" && method === "POST") {
      calls.create++;
      const payload = req.postDataJSON() as {
        title: string;
        body: string;
        tags?: string[];
        student_id?: string | null;
      };
      lastBody.create = payload;
      const created = seedNote({
        title: payload.title,
        body: payload.body,
        tags: payload.tags ?? [],
        student_id: payload.student_id ?? null,
      });
      notes.unshift(created);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(created),
      });
      return;
    }

    const promoteMatch = pathname.match(/^\/notes\/([^/]+)\/promote$/);
    if (promoteMatch && method === "POST") {
      calls.promote++;
      const note = notes.find((n) => n.id === promoteMatch[1]);
      if (!note) {
        await route.fulfill({
          status: 404,
          contentType: "application/json",
          headers: CORS_HEADERS,
          body: JSON.stringify({ detail: "note not found" }),
        });
        return;
      }
      if (note.promoted_to_knowledge) {
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          headers: CORS_HEADERS,
          body: JSON.stringify({ detail: "note already promoted to knowledge" }),
        });
        return;
      }
      note.promoted_to_knowledge = true;
      note.updated_at = new Date().toISOString();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({ ...note, source_id: randomUUID() }),
      });
      return;
    }

    const patchMatch = pathname.match(/^\/notes\/([^/]+)$/);
    if (patchMatch && method === "PATCH") {
      calls.update++;
      const payload = req.postDataJSON();
      lastBody.update = payload;
      const note = notes.find((n) => n.id === patchMatch[1]);
      if (note) {
        if (typeof payload.title === "string") note.title = payload.title;
        if (typeof payload.body === "string") note.body = payload.body;
        if (Array.isArray(payload.tags)) note.tags = payload.tags;
        if (payload.student_id !== null && payload.student_id !== undefined) {
          note.student_id = payload.student_id;
        }
        note.updated_at = new Date().toISOString();
      }
      await route.fulfill({
        status: note ? 200 : 404,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(note ?? { detail: "note not found" }),
      });
      return;
    }

    if (patchMatch && method === "DELETE") {
      calls.delete++;
      const idx = notes.findIndex((n) => n.id === patchMatch[1]);
      if (idx >= 0) notes.splice(idx, 1);
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  }

  async function studentsHandler(route: Route) {
    if (route.request().method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    calls.studentsList++;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS_HEADERS,
      body: JSON.stringify(students),
    });
  }

  await page.route(`${API_ORIGIN}/notes`, notesHandler);
  await page.route(`${API_ORIGIN}/notes/**`, notesHandler);
  await page.route(`${API_ORIGIN}/students`, studentsHandler);

  return { notes, calls, lastBody, unexpected };
}

test.describe("notes cockpit (mocked API)", () => {
  test("shows the empty state, adds a note (with tags + student), and promotes it", async ({ page }) => {
    const student = seedStudent({ name: "Maria Ioannou" });
    const mock = await mockNotesApi(page, { notes: [], students: [student] });

    await page.goto("/en/notes");

    await expect(page.getByTestId("notes-heading")).toBeVisible();
    await expect(page.getByTestId("notes-empty")).toBeVisible();
    expect(mock.calls.list).toBeGreaterThanOrEqual(1);

    // The student picker is populated from GET /students before we use it.
    const studentSelect = page.getByTestId("note-student-select");
    await expect(studentSelect).toContainText("Maria Ioannou");

    // Create -> POST /notes with the right payload -> appears in the list.
    await page.getByTestId("note-title-input").fill("Barre chord tip");
    await page.getByTestId("note-body-input").fill("Keep the thumb centered behind the neck.");
    await page.getByTestId("note-tags-input").fill("technique, chords");
    await studentSelect.selectOption({ label: "Maria Ioannou" });
    await page.getByTestId("note-form-submit").click();

    const row = page.getByTestId("note-item").filter({ hasText: "Barre chord tip" });
    await expect(row).toBeVisible();
    await expect(page.getByTestId("notes-empty")).toHaveCount(0);

    expect(mock.calls.create).toBe(1);
    const createBody = mock.lastBody.create as {
      title: string;
      body: string;
      tags?: string[];
      student_id?: string | null;
    };
    expect(createBody.title).toBe("Barre chord tip");
    expect(createBody.body).toBe("Keep the thumb centered behind the neck.");
    expect(createBody.tags).toEqual(["technique", "chords"]);
    expect(createBody.student_id).toBe(student.id);

    await expect(row.getByTestId("note-tag")).toHaveText(["technique", "chords"]);
    await expect(row.getByTestId("note-student")).toHaveText("Maria Ioannou");

    // Not yet promoted: the action shows, no "promoted" badge yet.
    await expect(row.getByTestId("note-promote")).toBeVisible();
    await expect(row.getByTestId("note-promoted-badge")).toHaveCount(0);

    // Promote -> POST /notes/{id}/promote -> the row flips to a promoted
    // state and the action is replaced (never a second POST from this UI).
    await row.getByTestId("note-promote").click();
    await expect(row.getByTestId("note-promoted-badge")).toBeVisible();
    await expect(row.getByTestId("note-promote")).toHaveCount(0);
    expect(mock.calls.promote).toBe(1);

    expect(mock.unexpected).toEqual([]);
  });

  test("edits a note's title via the per-note edit form", async ({ page }) => {
    const seed = seedNote({ title: "Original title", body: "Original body" });
    const mock = await mockNotesApi(page, { notes: [seed] });

    await page.goto("/en/notes");

    const row = page.getByTestId("note-item").filter({ hasText: "Original title" });
    await expect(row).toBeVisible();

    await row.getByTestId("note-edit").click();
    const editForm = page.getByTestId("note-edit-form");
    await expect(editForm).toBeVisible();
    await expect(editForm.getByTestId("note-title-input")).toHaveValue("Original title");

    await editForm.getByTestId("note-title-input").fill("Updated title");
    await editForm.getByTestId("note-form-submit").click();

    await expect(page.getByTestId("note-edit-form")).toHaveCount(0);
    await expect(page.getByTestId("note-item").filter({ hasText: "Updated title" })).toBeVisible();
    expect(mock.calls.update).toBe(1);
    expect((mock.lastBody.update as { title?: string }).title).toBe("Updated title");

    expect(mock.unexpected).toEqual([]);
  });

  test("deletes a note from the list", async ({ page }) => {
    const seed = seedNote({ title: "Delete Me" });
    const mock = await mockNotesApi(page, { notes: [seed] });

    await page.goto("/en/notes");

    const row = page.getByTestId("note-item").filter({ hasText: "Delete Me" });
    await expect(row).toBeVisible();

    await row.getByTestId("note-delete").click();
    // Guarded since Stage 7.1 — the DELETE only fires once the confirm dialog
    // is accepted (the guard itself is covered exhaustively in confirm.spec.ts).
    await page.getByTestId("confirm-accept").click();
    await expect(page.getByTestId("note-item").filter({ hasText: "Delete Me" })).toHaveCount(0);
    await expect(page.getByTestId("notes-empty")).toBeVisible();
    expect(mock.calls.delete).toBe(1);
    expect(mock.unexpected).toEqual([]);
  });
});
