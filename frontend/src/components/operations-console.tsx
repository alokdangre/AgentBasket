"use client";

import { useState } from "react";

import {
  apiErrorMessage,
  type ApiError,
  type InventoryRow,
  type OperationsDashboard,
  type OperationsOrder,
} from "@/lib/account-types";
import { formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/account.module.css";

const transitions: Record<OperationsOrder["status"], OperationsOrder["status"][]> = {
  awaiting_payment: ["canceled"],
  paid: ["preparing", "canceled"],
  preparing: ["ready", "canceled"],
  ready: ["fulfilled", "canceled"],
  fulfilled: [],
  canceled: [],
};

const labels: Record<OperationsOrder["status"], string> = {
  awaiting_payment: "Awaiting payment",
  paid: "Paid",
  preparing: "Preparing",
  ready: "Ready",
  fulfilled: "Fulfilled",
  canceled: "Canceled",
};

type OperationsConsoleProps = {
  initialDashboard: OperationsDashboard;
  canAdjustInventory: boolean;
};

export function OperationsConsole({
  initialDashboard,
  canAdjustInventory,
}: OperationsConsoleProps) {
  const [dashboard, setDashboard] = useState(initialDashboard);
  const [editingInventory, setEditingInventory] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function updateOrder(order: OperationsOrder, status: OperationsOrder["status"]) {
    setPending(order.id);
    setMessage(null);
    try {
      const response = await fetch(`/api/commerce/merchant/operations/orders/${order.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      const result = (await response.json()) as OperationsOrder & ApiError;
      if (response.ok) {
        setDashboard((current) => ({
          ...current,
          recent_orders: current.recent_orders.map((row) =>
            row.id === result.id ? result : row,
          ),
          summary: {
            ...current.summary,
            orders_preparing:
              current.summary.orders_preparing +
              (result.status === "preparing" ? 1 : 0) -
              (order.status === "preparing" ? 1 : 0),
          },
        }));
        setMessage(`${result.public_number} moved to ${labels[result.status].toLocaleLowerCase()}.`);
      } else {
        setMessage(apiErrorMessage(result, "Could not update the order."));
      }
    } catch {
      setMessage("The service is unavailable. Try again shortly.");
    } finally {
      setPending(null);
    }
  }

  async function updateInventory(row: InventoryRow, form: HTMLFormElement) {
    setPending(row.id);
    setMessage(null);
    try {
      const data = new FormData(form);
      const response = await fetch(`/api/commerce/merchant/operations/inventory/${row.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          on_hand_quantity: Number(data.get("on_hand_quantity")),
          reason: data.get("reason"),
        }),
      });
      const result = (await response.json()) as InventoryRow & ApiError;
      if (response.ok) {
        setDashboard((current) => ({
          ...current,
          inventory_attention: current.inventory_attention
            .map((item) => (item.id === result.id ? result : item))
            .filter((item) => item.available_quantity <= item.reorder_point),
          summary: {
            ...current.summary,
            low_stock_variants:
              result.available_quantity > result.reorder_point
                ? Math.max(0, current.summary.low_stock_variants - 1)
                : current.summary.low_stock_variants,
          },
        }));
        setEditingInventory(null);
        setMessage(`${result.product_name} stock saved with an audit entry.`);
      } else {
        setMessage(apiErrorMessage(result, "Could not update inventory."));
      }
    } catch {
      setMessage("The service is unavailable. Try again shortly.");
    } finally {
      setPending(null);
    }
  }

  const { summary } = dashboard;
  return (
    <div className={styles.operationsContent}>
      <header className={styles.operationsTitle}>
        <h1>Today at Ember &amp; Leaf</h1>
      </header>
      <dl className={styles.operationsSummary}>
        <div>
          <dt>Open checkouts</dt>
          <dd>{summary.open_checkouts}</dd>
        </div>
        <div>
          <dt>Orders preparing</dt>
          <dd>{summary.orders_preparing}</dd>
        </div>
        <div>
          <dt>Low-stock variants</dt>
          <dd>{summary.low_stock_variants}</dd>
        </div>
        <div>
          <dt>Captured revenue</dt>
          <dd>{formatMoney(summary.captured_revenue_minor, summary.currency)}</dd>
        </div>
      </dl>
      <section id="commerce" className={styles.operationsSection}>
        <div className={styles.operationsSectionHeading}>
          <h2>Agentic commerce readiness</h2>
          <span>Live capabilities only; secrets are never shown.</span>
        </div>
        <div className={styles.readinessGrid}>
          <div className={styles.readinessGroup}>
            <h3>Protocols</h3>
            {dashboard.commerce_readiness.protocols.map((protocol) => (
              <article key={protocol.name} className={styles.readinessRow}>
                <div>
                  <strong>{protocol.name}</strong>
                  {protocol.version ? <small>{protocol.version}</small> : null}
                </div>
                <span className={styles.readinessStatus} data-status={protocol.status}>
                  {protocol.status.replaceAll("_", " ")}
                </span>
                <p>{protocol.detail}</p>
                <a href={protocol.endpoint} target="_blank" rel="noreferrer">
                  Inspect endpoint
                </a>
              </article>
            ))}
          </div>
          <div className={styles.readinessGroup}>
            <h3>Payment rails</h3>
            {dashboard.commerce_readiness.payments.map((payment) => (
              <article key={payment.name} className={styles.readinessRow}>
                <div>
                  <strong>{payment.name}</strong>
                  <small>{payment.mode}</small>
                </div>
                <span className={styles.readinessStatus} data-status={payment.status}>
                  {payment.status}
                </span>
                <p>{payment.detail}</p>
              </article>
            ))}
          </div>
        </div>
        {dashboard.commerce_readiness.warnings.length ? (
          <ul className={styles.readinessWarnings}>
            {dashboard.commerce_readiness.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        ) : null}
      </section>
      <section id="orders" className={styles.operationsSection}>
        <div className={styles.operationsSectionHeading}>
          <h2>Recent orders</h2>
          <span>Transitions are logged; payment status is read-only.</span>
        </div>
        {dashboard.recent_orders.length ? (
          <div className={styles.tableScroll}>
            <table>
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Customer</th>
                  <th>Fulfillment</th>
                  <th>Total</th>
                  <th>Status</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {dashboard.recent_orders.map((order) => (
                  <tr key={order.id}>
                    <td>{order.public_number}</td>
                    <td>{order.customer_name ?? "Guest"}</td>
                    <td>{order.fulfillment_type.replaceAll("_", " ")}</td>
                    <td>{formatMoney(order.total_minor, order.currency)}</td>
                    <td>
                      {transitions[order.status].length ? (
                        <select
                          value={order.status}
                          disabled={pending === order.id}
                          aria-label={`Status for ${order.public_number}`}
                          onChange={(event) =>
                            updateOrder(order, event.target.value as OperationsOrder["status"])
                          }
                        >
                          <option value={order.status}>{labels[order.status]}</option>
                          {transitions[order.status].map((status) => (
                            <option key={status} value={status}>
                              Move to {labels[status]}
                            </option>
                          ))}
                        </select>
                      ) : (
                        labels[order.status]
                      )}
                    </td>
                    <td>{new Date(order.created_at).toLocaleString("en-IN")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className={styles.emptyCopy}>Orders will appear after checkout and payment.</p>
        )}
      </section>
      <section id="inventory" className={styles.operationsSection}>
        <div className={styles.operationsSectionHeading}>
          <h2>Inventory attention</h2>
          <span>Available stock at or below its reorder point.</span>
        </div>
        {dashboard.inventory_attention.length ? (
          <div className={styles.tableScroll}>
            <table>
              <thead>
                <tr>
                  <th>Variant</th>
                  <th>Location</th>
                  <th>On hand</th>
                  <th>Reserved</th>
                  <th>Available</th>
                  <th>Reorder point</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {dashboard.inventory_attention.map((row) => (
                  <tr key={row.id}>
                    <td>
                      {row.product_name} · {row.variant_name}
                    </td>
                    <td>{row.location_name}</td>
                    <td>{row.on_hand_quantity}</td>
                    <td>{row.reserved_quantity}</td>
                    <td>{row.available_quantity}</td>
                    <td>{row.reorder_point}</td>
                    <td>
                      {editingInventory === row.id ? (
                        <form
                          className={styles.inlineInventoryForm}
                          onSubmit={(event) => {
                            event.preventDefault();
                            updateInventory(row, event.currentTarget);
                          }}
                        >
                          <input
                            name="on_hand_quantity"
                            type="number"
                            min={row.reserved_quantity}
                            defaultValue={row.on_hand_quantity}
                            aria-label="New on-hand quantity"
                            required
                          />
                          <input name="reason" minLength={3} placeholder="Reason" required />
                          <button type="submit" disabled={pending === row.id}>
                            Save
                          </button>
                          <button type="button" onClick={() => setEditingInventory(null)}>
                            Cancel
                          </button>
                        </form>
                      ) : canAdjustInventory ? (
                        <button type="button" onClick={() => setEditingInventory(row.id)}>
                          Update stock
                        </button>
                      ) : (
                        "Admin only"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className={styles.emptyCopy}>No variants currently need attention.</p>
        )}
      </section>
      {message ? <p className={styles.operationsMessage}>{message}</p> : null}
    </div>
  );
}
