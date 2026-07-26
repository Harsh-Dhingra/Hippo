import { api } from "@/lib/api";

type SkillInput = {
  name: string;
  description: string;
  required: boolean;
  default: string | null;
};

type Skill = {
  name: string;
  version: string;
  description: string;
  inputs: SkillInput[];
  proposes: boolean;
  actions: string[];
};

/**
 * Skills, as a list of forms.
 *
 * A skill that can propose an action is marked, because "this will answer" and
 * "this may ask you to approve something" are different enough that a person
 * should know which they clicked before they click it — not after.
 */
export default async function SkillsPage() {
  const skills = await api.get<Skill[]>("/api/v1/skills");

  if (skills.length === 0) {
    return (
      <div className="mx-auto max-w-2xl">
        <h1 className="text-xl font-semibold tracking-tight">Skills</h1>
        <p className="mt-3 text-sm text-[var(--color-muted)]">
          None are installed. A skill is a YAML file describing a question your team asks
          often. Point <code>HIPPO_SKILLS_PATH</code> at a directory of them.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl">
      <h1 className="text-xl font-semibold tracking-tight">Skills</h1>
      <p className="mt-2 text-sm text-[var(--color-muted)]">
        Each one runs as you. You will see what your accounts can see, and nothing else.
      </p>

      <ul className="mt-6 space-y-6">
        {skills.map((skill) => (
          <li key={skill.name} className="rounded border border-[var(--color-line)] p-4">
            <div className="flex items-baseline justify-between gap-3">
              <h2 className="font-medium">{skill.name}</h2>
              <span className="text-xs text-[var(--color-muted)]">v{skill.version}</span>
            </div>
            <p className="mt-1 text-sm text-[var(--color-muted)]">{skill.description}</p>

            {skill.proposes ? (
              <p className="mt-2 text-xs text-[var(--color-warn)]">
                May propose: {skill.actions.join(", ")}. Nothing is executed without your
                approval.
              </p>
            ) : null}

            <form action="/api/skills" method="post" className="mt-4 space-y-3">
              <input type="hidden" name="skill" value={skill.name} />
              {skill.inputs.map((input) => (
                <label key={input.name} className="block">
                  <span className="text-sm font-medium">{input.name}</span>
                  {input.description ? (
                    <span className="ml-2 text-xs text-[var(--color-muted)]">
                      {input.description}
                    </span>
                  ) : null}
                  <input
                    type="text"
                    name={`input.${input.name}`}
                    required={input.required && input.default === null}
                    defaultValue={input.default ?? ""}
                    className="mt-1 w-full rounded border border-[var(--color-line)] bg-white px-3 py-2 text-sm"
                  />
                </label>
              ))}
              <button
                type="submit"
                className="rounded bg-[var(--color-accent)] px-3 py-2 text-sm font-medium text-white"
              >
                Run
              </button>
            </form>
          </li>
        ))}
      </ul>
    </div>
  );
}
