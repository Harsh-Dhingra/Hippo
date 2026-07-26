import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Hippo",
  description: "Permission-aware memory over your team's tools.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <header className="border-b border-[var(--color-line)] bg-white">
          <nav className="mx-auto flex max-w-4xl items-center gap-6 px-6 py-4 text-sm">
            <Link href="/ask" className="font-semibold tracking-tight">
              Hippo
            </Link>
            <Link href="/ask" className="text-[var(--color-muted)] hover:text-[var(--color-ink)]">
              Ask
            </Link>
            <Link
              href="/notes"
              className="text-[var(--color-muted)] hover:text-[var(--color-ink)]"
            >
              Memory
            </Link>
            <Link
              href="/actions"
              className="text-[var(--color-muted)] hover:text-[var(--color-ink)]"
            >
              Actions
            </Link>
            <Link
              href="/traces"
              className="text-[var(--color-muted)] hover:text-[var(--color-ink)]"
            >
              Traces
            </Link>
            <form action="/api/session/logout" method="post" className="ml-auto">
              <button
                type="submit"
                className="text-[var(--color-muted)] hover:text-[var(--color-ink)]"
              >
                Sign out
              </button>
            </form>
          </nav>
        </header>
        <main className="mx-auto max-w-4xl px-6 py-10">{children}</main>
      </body>
    </html>
  );
}
