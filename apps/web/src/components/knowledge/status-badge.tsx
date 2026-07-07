"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import type { SourceStatus } from "@/lib/api";

const VARIANT_BY_STATUS = {
  ready: "default",
  ingesting: "secondary",
  failed: "destructive",
} as const;

const KNOWN_STATUSES = Object.keys(VARIANT_BY_STATUS) as SourceStatus[];

function isKnownStatus(status: string): status is SourceStatus {
  return (KNOWN_STATUSES as string[]).includes(status);
}

/** Small status pill for a `KnowledgeSource.status` value (ingesting/ready/failed),
 * translated per-locale; falls back to the raw string for any future/unknown status
 * so this never throws on an unexpected value from the API. */
export function StatusBadge({ status }: { status: string }) {
  const t = useTranslations("knowledge.status");
  const variant = isKnownStatus(status) ? VARIANT_BY_STATUS[status] : "outline";
  const label = isKnownStatus(status) ? t(status) : status;
  return (
    <Badge data-testid="status-badge" data-status={status} variant={variant}>
      {label}
    </Badge>
  );
}
