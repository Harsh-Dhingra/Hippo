"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { ApiError, NotAuthenticated, api, type Answer } from "@/lib/api";

export type AskState = { answer: Answer | null; error: string | null };

export async function ask(_previous: AskState, form: FormData): Promise<AskState> {
  const question = String(form.get("question") ?? "").trim();
  if (!question) return { answer: null, error: "Ask something first." };

  try {
    const answer = await api.post<Answer>("/api/v1/queries", { question, k: 20 });
    revalidatePath("/actions");
    return { answer, error: null };
  } catch (error) {
    if (error instanceof NotAuthenticated) redirect("/login");
    return {
      answer: null,
      error: error instanceof ApiError ? error.message : "Something went wrong.",
    };
  }
}
