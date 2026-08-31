"use client";

import Link from "next/link";
import {
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { AgentMessageView } from "@/components/agent-message";
import { ChatIcon, CloseIcon } from "@/components/icons";
import {
  apiErrorMessage,
  type AgentConversation,
  type AgentMessage,
  type AgentTurn,
  type ApiError,
} from "@/lib/account-types";
import styles from "@/styles/storefront.module.css";

type ChatStatus = "idle" | "loading" | "sending";

export function ChatLauncher() {
  const [open, setOpen] = useState(false);
  const [conversation, setConversation] = useState<AgentConversation | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [status, setStatus] = useState<ChatStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  const loadCurrentConversation = useCallback(async () => {
    setStatus("loading");
    setError(null);
    try {
      const response = await fetch("/api/commerce/agent/conversations/current", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merchant_slug: "ember-and-leaf" }),
      });
      const payload = (await response.json()) as AgentConversation & ApiError;
      if (!response.ok) {
        setNeedsSignIn(response.status === 401);
        setError(apiErrorMessage(payload, "Ask Ember could not start."));
        return;
      }
      setConversation(payload);
      setMessages(payload.messages);
      setNeedsSignIn(false);
    } catch {
      setError("Ask Ember cannot reach the commerce service right now.");
    } finally {
      setStatus("idle");
    }
  }, []);

  const openAgent = useCallback(() => {
    setOpen(true);
    if (!conversation && status === "idle") void loadCurrentConversation();
  }, [conversation, loadCurrentConversation, status]);

  useEffect(() => {
    const handleOpen = () => openAgent();
    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("ask-ember:open", handleOpen);
    window.addEventListener("keydown", handleEscape);
    return () => {
      window.removeEventListener("ask-ember:open", handleOpen);
      window.removeEventListener("keydown", handleEscape);
    };
  }, [openAgent]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages, status]);

  async function createNewConversation() {
    if (status !== "idle") return;
    setStatus("loading");
    setError(null);
    try {
      const response = await fetch("/api/commerce/agent/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merchant_slug: "ember-and-leaf" }),
      });
      const payload = (await response.json()) as AgentConversation & ApiError;
      if (!response.ok) {
        setError(apiErrorMessage(payload, "Could not start a new conversation."));
        return;
      }
      setConversation(payload);
      setMessages(payload.messages);
      setDraft("");
    } catch {
      setError("Could not start a new conversation.");
    } finally {
      setStatus("idle");
    }
  }

  async function refreshConversation(conversationId: string) {
    try {
      const response = await fetch(`/api/commerce/agent/conversations/${conversationId}`, {
        cache: "no-store",
      });
      if (!response.ok) return;
      const payload = (await response.json()) as AgentConversation;
      setConversation(payload);
      setMessages(payload.messages);
    } catch {
      // The visible error remains the useful recovery instruction.
    }
  }

  async function sendMessage(content: string) {
    const normalized = content.trim();
    if (!normalized || !conversation || status !== "idle") return;
    const optimistic: AgentMessage = {
      id: `pending-${crypto.randomUUID()}`,
      role: "user",
      content: normalized,
      structured_content: {},
      model: null,
      created_at: new Date().toISOString(),
    };
    setMessages((current) => [...current, optimistic]);
    setDraft("");
    setStatus("sending");
    setError(null);
    try {
      const response = await fetch(
        `/api/commerce/agent/conversations/${conversation.id}/messages`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: JSON.stringify({ content: normalized }),
        },
      );
      const payload = (await response.json()) as AgentTurn & ApiError;
      if (!response.ok || !("message" in payload)) {
        setError(apiErrorMessage(payload, "Ask Ember could not answer safely."));
        await refreshConversation(conversation.id);
        return;
      }
      setMessages((current) => [...current, payload.message]);
      if (payload.message.structured_content.cart) {
        window.dispatchEvent(new Event("cart:updated"));
      }
    } catch {
      setError("The response was interrupted. Reopen the chat to refresh its audit state.");
      await refreshConversation(conversation.id);
    } finally {
      setStatus("idle");
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void sendMessage(draft);
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendMessage(draft);
    }
  }

  return (
    <aside id="ask-ember" className={styles.chatWrap}>
      {open ? (
        <section
          className={styles.chatPanel}
          role="dialog"
          aria-label="Ask Ember shopping assistant"
        >
          <header className={styles.chatHeading}>
            <div>
              <strong>Ask Ember</strong>
              <span>Live catalog · approval-gated checkout</span>
            </div>
            <div className={styles.chatHeadingActions}>
              {conversation ? (
                <button
                  type="button"
                  className={styles.chatNewButton}
                  disabled={status !== "idle"}
                  onClick={() => void createNewConversation()}
                >
                  New
                </button>
              ) : null}
              <button
                type="button"
                aria-label="Close Ask Ember"
                onClick={() => setOpen(false)}
              >
                <CloseIcon />
              </button>
            </div>
          </header>

          <div className={styles.agentMessages} aria-live="polite">
            {messages.map((message) => (
              <AgentMessageView
                key={message.id}
                message={message}
                disabled={status !== "idle"}
                onSuggestion={(suggestion) => void sendMessage(suggestion)}
                onPaid={() => {
                  window.dispatchEvent(new Event("cart:updated"));
                  if (conversation) void refreshConversation(conversation.id);
                }}
              />
            ))}
            {status === "loading" ? <p className={styles.agentStatus}>Opening your chat…</p> : null}
            {status === "sending" ? (
              <p className={styles.agentStatus}>Ember is checking the commerce core…</p>
            ) : null}
            {error ? (
              <div className={styles.agentError} role="status">
                <p>{error}</p>
                {needsSignIn ? <Link href="/account/sign-in?next=/">Sign in to chat</Link> : null}
              </div>
            ) : null}
            <div ref={endRef} />
          </div>

          <form className={styles.agentComposer} onSubmit={submit}>
            <label className={styles.srOnly} htmlFor="ask-ember-message">
              Message Ask Ember
            </label>
            <textarea
              id="ask-ember-message"
              value={draft}
              maxLength={2000}
              rows={2}
              disabled={!conversation || status !== "idle"}
              placeholder="Ask for a recommendation or update your cart…"
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={handleComposerKeyDown}
            />
            <button
              type="submit"
              disabled={!conversation || status !== "idle" || !draft.trim()}
            >
              Send
            </button>
          </form>
          <p className={styles.agentBoundary}>
            Ember may update your cart. Approval and payment always stay with you.
          </p>
        </section>
      ) : null}
      <button
        type="button"
        className={styles.chatButton}
        aria-label={open ? "Close Ask Ember" : "Open Ask Ember"}
        aria-expanded={open}
        onClick={() => (open ? setOpen(false) : openAgent())}
      >
        {open ? <CloseIcon /> : <ChatIcon />}
      </button>
    </aside>
  );
}
