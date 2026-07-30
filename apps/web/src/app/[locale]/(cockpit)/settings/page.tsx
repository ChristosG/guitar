"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, Eye, EyeOff, Loader2, TriangleAlert } from "lucide-react";

import {
  getSettings,
  saveSettings,
  testSettings,
  type AppSettings,
  type LlmModel,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PromptList } from "@/components/settings/prompt-list";
import { BlueprintDefaultCard } from "@/components/settings/blueprint-default-card";
import { BackupCard } from "@/components/settings/backup-card";
import { cn } from "@/lib/utils";

const CONSOLE_URL = "https://console.anthropic.com/settings/keys";

/** THE TUTOR IS A TOTAL BEGINNER WITH COMPUTERS. Everything on this page follows
 * from that one fact:
 *
 *  - He never sees JSON, a stack trace, a status code, or an English string we
 *    forgot to translate. Every failure the server can produce arrives as a
 *    machine-readable `code`, and this component turns it into exactly one
 *    sentence from `settings.errors.*` in his own locale — plus a link to
 *    console.anthropic.com, because for most of those codes the fix is over
 *    there, not here.
 *  - The key field is `type="password"` with a Show toggle, `autoComplete="off"`
 *    and `spellCheck={false}` — a paste target, not a thing to type.
 *  - The saved key is shown as `sk-ant-…7f2a`, where the API supplied only the
 *    `7f2a` (see `AppSettings.key_hint`). The prefix is a constant we render.
 *  - Test is a separate button from Save, because a green check has to MEAN
 *    something: the server does a real generation behind it, not a key parse.
 */
export default function SettingsPage() {
  const t = useTranslations("settings");

  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [keyInput, setKeyInput] = useState("");
  const [reveal, setReveal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testOk, setTestOk] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSettings(await getSettings());
    } catch {
      setErrorCode("network");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** Any interaction invalidates a previous verdict. A green check left standing
   * next to a key the tutor has since edited is a lie with a tick next to it. */
  function reset() {
    setSaved(false);
    setTestOk(false);
    setErrorCode(null);
  }

  async function onSave() {
    setSaving(true);
    reset();
    try {
      const next = await saveSettings({ anthropic_key: keyInput.trim() });
      setSettings(next);
      setKeyInput("");
      setSaved(true);
    } catch {
      setErrorCode("save_failed");
    } finally {
      setSaving(false);
    }
  }

  async function onPickModel(model: LlmModel) {
    if (settings?.model === model) return;
    reset();
    // Optimistic: the radio must move under the finger. A failed PUT reverts it.
    const previous = settings;
    setSettings((s) => (s ? { ...s, model } : s));
    try {
      setSettings(await saveSettings({ model }));
    } catch {
      setSettings(previous);
      setErrorCode("save_failed");
    }
  }

  async function onTest() {
    setTesting(true);
    reset();
    try {
      // An unsaved key in the box is what the tutor means to test — paste, Test,
      // Save is the natural order, and making him save first would persist a key
      // he has no reason yet to believe in.
      const result = await testSettings(keyInput.trim() || undefined);
      if (result.ok) setTestOk(true);
      else setErrorCode(result.code ?? "generation_failed");
    } catch {
      setErrorCode("network");
    } finally {
      setTesting(false);
    }
  }

  const models: { id: LlmModel; name: string; note: string }[] = [
    { id: "claude-sonnet-5", name: t("sonnetName"), note: t("sonnetNote") },
    { id: "claude-haiku-4-5", name: t("haikuName"), note: t("haikuNote") },
  ];

  return (
    <div className="flex max-w-2xl flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 className="font-heading text-lg font-semibold">{t("heading")}</h2>
        <p className="text-sm text-muted-foreground">{t("subheading")}</p>
      </header>

      {/* --- the key ---------------------------------------------------- */}
      <Card data-testid="settings-key-card">
        <CardHeader>
          <CardTitle>{t("keyTitle")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-sm text-muted-foreground">{t("keyHelp")}</p>

          {settings?.key_hint && (
            <p className="text-sm" data-testid="settings-key-hint">
              {t("keySaved", { hint: `sk-ant-…${settings.key_hint}` })}
            </p>
          )}

          <Label htmlFor="anthropic-key">{t("keyLabel")}</Label>
          <div className="relative">
            <Input
              id="anthropic-key"
              data-testid="settings-key-input"
              type={reveal ? "text" : "password"}
              autoComplete="off"
              spellCheck={false}
              placeholder={t("keyPlaceholder")}
              value={keyInput}
              onChange={(e) => {
                setKeyInput(e.target.value);
                reset();
              }}
              className="h-10 pr-10 font-mono"
            />
            <button
              type="button"
              onClick={() => setReveal((v) => !v)}
              aria-label={reveal ? t("hide") : t("show")}
              data-testid="settings-key-reveal"
              className="absolute top-1/2 right-2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              {reveal ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
            </button>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              onClick={onSave}
              disabled={saving || keyInput.trim().length === 0}
              data-testid="settings-save"
            >
              {saving && <Loader2 className="size-4 animate-spin" />}
              {saving ? t("saving") : t("save")}
            </Button>
            <Button
              variant="outline"
              onClick={onTest}
              disabled={testing}
              data-testid="settings-test"
            >
              {testing && <Loader2 className="size-4 animate-spin" />}
              {testing ? t("testing") : t("test")}
            </Button>

            {saved && !testOk && (
              <span className="text-sm text-muted-foreground" data-testid="settings-saved">
                {t("saved")}
              </span>
            )}
            {testOk && (
              <span
                className="flex items-center gap-1.5 text-sm font-medium text-emerald-600 dark:text-emerald-400"
                data-testid="settings-test-ok"
              >
                <Check className="size-4" />
                {t("testOk")}
              </span>
            )}
          </div>

          {errorCode && (
            <div
              role="alert"
              data-testid="settings-error"
              className="flex items-start gap-2 rounded-lg bg-destructive/10 p-3 text-sm text-destructive"
            >
              <TriangleAlert className="mt-0.5 size-4 shrink-0" />
              <div className="flex flex-col gap-1">
                {/* `t.has` keeps an unknown future server code from rendering the
                    raw key path (`settings.errors.some_new_code`) at the tutor. */}
                <span>
                  {t.has(`errors.${errorCode}`)
                    ? t(`errors.${errorCode}`)
                    : t("errors.generation_failed")}
                </span>
                <a
                  href={CONSOLE_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="w-fit underline underline-offset-4"
                >
                  {t("getKey")}
                </a>
              </div>
            </div>
          )}

          {!errorCode && (
            <a
              href={CONSOLE_URL}
              target="_blank"
              rel="noreferrer"
              className="w-fit text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground"
            >
              {t("getKey")}
            </a>
          )}
        </CardContent>
      </Card>

      {/* --- the model --------------------------------------------------- */}
      <Card data-testid="settings-model-card">
        <CardHeader>
          <CardTitle>{t("modelTitle")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-sm text-muted-foreground">{t("modelHelp")}</p>
          <div className="grid gap-3 sm:grid-cols-2" role="radiogroup" aria-label={t("modelTitle")}>
            {models.map((m) => {
              const active = settings?.model === m.id;
              return (
                <button
                  key={m.id}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => onPickModel(m.id)}
                  data-testid={`settings-model-${m.id}`}
                  className={cn(
                    "flex flex-col gap-1.5 rounded-xl p-4 text-left ring-1 transition-colors",
                    active
                      ? "bg-primary/5 ring-2 ring-primary"
                      : "ring-foreground/10 hover:bg-muted",
                  )}
                >
                  <span className="flex items-center gap-2 font-medium">
                    <span
                      className={cn(
                        "flex size-4 shrink-0 items-center justify-center rounded-full border",
                        active ? "border-primary bg-primary" : "border-input",
                      )}
                    >
                      {active && <Check className="size-3 text-primary-foreground" />}
                    </span>
                    {m.name}
                  </span>
                  <span className="text-sm text-muted-foreground">{m.note}</span>
                </button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      {/* --- the prompts -------------------------------------------------
       * The first thing Chris ever asked for, and the reason this page is
       * worth more than a key box: what the app actually TELLS the model,
       * verbatim, with a Greek account of it beside the text rather than
       * rewritten into it.
       *
       * `provider` is passed down rather than fetched again: this page already
       * knows it, and the list needs it to hide a prompt only the OTHER
       * connection sends. `null` while settings load, which hides the one
       * provider-specific card for that moment — the conservative direction,
       * since showing a prompt the app is not sending is the failure this whole
       * feature exists to prevent. */}
      <PromptList provider={settings?.provider ?? null} />

      {/* --- the lesson blueprint -----------------------------------------
       * The 8-section lesson skeleton (Plan C, Task 6), now tutor-editable —
       * what every NEW curriculum is seeded with. Editing it never touches a
       * course that already exists (spec invariant #3); the card says so. */}
      <BlueprintDefaultCard />

      {/* --- backup / restore ---------------------------------------------
       * Everything (database + page scans) as one downloadable archive, and
       * the way back from it — the same format the desktop seed bundle uses
       * (workstream A3, `app/routers/backup.py`). */}
      <BackupCard />
    </div>
  );
}
