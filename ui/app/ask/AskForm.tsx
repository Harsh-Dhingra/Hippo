"use client";

import { useActionState } from "react";
import Link from "next/link";
import { Citations } from "@/components/Citations";
import { ask, type AskState } from "./actions";

const EMPTY: AskState = { answer: null, error: null };

export function AskForm() {
  const [state, submit, pending] = useActionState(ask, EMPTY);
  const answer = state.answer;

  return (
    <>
      <form action={submit} className="flex gap-2">
        <input
          type="text"
          name="question"
          required
          placeholder="What is blocking the Acme renewal?"
          className="flex-1 rounded border border-[var(--color-line)] bg-white px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={pending}
          className="rounded bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {pending ? "Asking…" : "Ask"}
        </button>
      </form>

      {state.error ? (
        <p role="alert" className="mt-4 text-sm text-[var(--color-warn)]">
          {state.error}
        </p>
      ) : null}

      {answer ? (
        <article className="mt-8 rounded border border-[var(--color-line)] bg-white p-6">
          <p className="whitespace-pre-wrap text-sm leading-relaxed">
            {answer.refused
              ? "The model declined to answer this one."
              : answer.answer}
          </p>

          <Citations citations={answer.citations} />

          {answer.proposal ? (
            <div className="mt-6 rounded border border-[var(--color-line)] bg-[#fffdf5] p-4">
              <p className="text-sm font-medium">{answer.proposal.summary}</p>
              <p className="mt-1 text-sm text-[var(--color-muted)]">
                Nothing has happened yet. This is waiting for your approval.
              </p>
              <Link
                href="/actions"
                className="mt-3 inline-block text-sm text-[var(--color-accent)] underline underline-offset-2"
              >
                Review it
              </Link>
            </div>
          ) : null}

          <footer className="mt-6 flex gap-4 border-t border-[var(--color-line)] pt-3 text-xs text-[var(--color-muted)]">
            <span>{answer.model}</span>
            <span>
              {answer.input_tokens + answer.output_tokens} tokens
            </span>
            {answer.trace_id ? (
              <Link
                href={`/traces/${answer.trace_id}`}
                className="ml-auto text-[var(--color-accent)] underline underline-offset-2"
              >
                How this answer was built
              </Link>
            ) : null}
          </footer>
        </article>
      ) : null}
    </>
  );
}
