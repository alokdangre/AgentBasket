import Link from "next/link";

import type { ScheduledPurchase } from "@/lib/account-types";
import { formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/storefront.module.css";

const STATUS_COPY: Record<ScheduledPurchase["status"], string> = {
  draft: "Draft only · passkey approval required",
  pending_provider_authorization: "AP2 approved · payment setup pending",
  active: "Bounded autonomous runs active",
  paused: "Autonomous runs paused",
  needs_attention: "Stopped · your attention is required",
  completed: "Schedule completed",
  expired: "Authorization expired",
  revoked: "Authorization revoked",
};

function cadence(schedule: ScheduledPurchase): string {
  if (schedule.frequency === "once") return "One scheduled order";
  const unit = { daily: "day", weekly: "week", monthly: "month" }[schedule.frequency];
  return schedule.interval_count === 1
    ? `Every ${unit}`
    : `Every ${schedule.interval_count} ${unit}s`;
}

export function ScheduledPurchaseArtifact({ schedule }: { schedule: ScheduledPurchase }) {
  const needsReview = schedule.status === "draft";

  return (
    <section className={styles.agentScheduleArtifact}>
      <header>
        <div>
          <span>Scheduled purchase</span>
          <strong>{cadence(schedule)}</strong>
        </div>
        <small data-status={schedule.status}>{STATUS_COPY[schedule.status]}</small>
      </header>
      <dl>
        <div>
          <dt>Per order</dt>
          <dd>{formatMoney(schedule.max_amount_minor, schedule.currency)}</dd>
        </div>
        <div>
          <dt>Total budget</dt>
          <dd>{formatMoney(schedule.max_total_minor, schedule.currency)}</dd>
        </div>
        <div>
          <dt>Maximum runs</dt>
          <dd>{schedule.max_occurrences}</dd>
        </div>
      </dl>
      <p>
        {needsReview
          ? "Nothing is authorized yet. Review the exact merchant, items, delivery, timing and money bounds before using your passkey."
          : "Your one-time authorization is stored with its AP2 evidence. Future purchases remain limited to those bounds."}
      </p>
      <Link href="/account#scheduled-purchases">
        {needsReview ? "Review exact authorization" : "Manage schedule and audit"}
      </Link>
    </section>
  );
}
