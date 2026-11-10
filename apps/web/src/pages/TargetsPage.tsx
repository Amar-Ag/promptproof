import { useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../api/client";
import type { ModelChoice, TargetFamily } from "../api/types";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useAsync } from "../hooks";

export function TargetsPage(): React.JSX.Element {
  const { result, reload } = useAsync<TargetFamily[]>(() => api.targets());
  const models = useAsync<ModelChoice[]>(() => api.models());

  return (
    <>
      <section className="panel">
        <h1>Targets</h1>
        <p className="lede">
          A target is the thing under test: a system prompt and the model it runs on. Targets
          are immutable — editing one creates a new version, so a report always refers to the
          exact prompt that produced it.
        </p>
      </section>

      <CreateTargetForm
        models={models.result.state === "loaded" ? models.result.data : []}
        onCreated={reload}
      />

      <section className="panel">
        <h2>Registered targets</h2>
        {result.state === "loading" ? <Loading what="targets" /> : null}
        {result.state === "error" ? (
          <ErrorState message={result.message} onRetry={reload} />
        ) : null}
        {result.state === "loaded" && result.data.length === 0 ? (
          <EmptyState
            title="No targets yet."
            hint="Register a system prompt above to run your first scan."
          />
        ) : null}
        {result.state === "loaded" && result.data.length > 0 ? (
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Kind</th>
                <th>Versions</th>
                <th>Created</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {result.data.map((family) => (
                <tr key={family.id}>
                  <td>{family.name}</td>
                  <td>{family.kind}</td>
                  <td>v{family.latest_version}</td>
                  <td>{new Date(family.created_at).toLocaleString()}</td>
                  <td>
                    <Link to={`/targets/${family.id}`}>Open</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </section>
    </>
  );
}

function CreateTargetForm({
  models,
  onCreated,
}: {
  models: ModelChoice[];
  onCreated: () => void;
}): React.JSX.Element {
  const [name, setName] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [model, setModel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const chosenModel = model || models[0]?.id || "";

  async function submit(event: React.FormEvent): Promise<void> {
    event.preventDefault();
    // Guarding on `submitting` is what stops a double click creating two targets.
    if (submitting) return;

    setSubmitting(true);
    setError(null);
    try {
      await api.createTarget({
        name,
        kind: "prompt",
        system_prompt: systemPrompt,
        model: chosenModel,
      });
      setName("");
      setSystemPrompt("");
      onCreated();
    } catch (caught: unknown) {
      setError(
        caught instanceof ApiError ? caught : new ApiError(0, "Something went wrong."),
      );
    } finally {
      setSubmitting(false);
    }
  }

  const fieldError = (field: string): string | undefined => error?.fieldErrors[field];

  return (
    <section className="panel">
      <h2>Register a prompt target</h2>
      <form onSubmit={submit} className="form" noValidate>
        <div className="form__row">
          <label htmlFor="target-name">Name</label>
          <input
            id="target-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={200}
            aria-invalid={fieldError("name") !== undefined}
            aria-describedby={fieldError("name") ? "target-name-error" : undefined}
          />
          {fieldError("name") ? (
            <p className="form__error" id="target-name-error">
              {fieldError("name")}
            </p>
          ) : null}
        </div>

        <div className="form__row">
          <label htmlFor="target-model">Model</label>
          <select
            id="target-model"
            value={chosenModel}
            onChange={(e) => setModel(e.target.value)}
          >
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
          {fieldError("model") ? <p className="form__error">{fieldError("model")}</p> : null}
        </div>

        <div className="form__row">
          <label htmlFor="target-prompt">System prompt</label>
          <textarea
            id="target-prompt"
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            rows={8}
            maxLength={20000}
            placeholder="Paste the system prompt you want to test."
            aria-invalid={fieldError("system_prompt") !== undefined}
            aria-describedby={
              fieldError("system_prompt") ? "target-prompt-error" : "target-prompt-hint"
            }
          />
          <p className="form__hint" id="target-prompt-hint">
            {systemPrompt.length.toLocaleString()} / 20,000 characters
          </p>
          {fieldError("system_prompt") ? (
            <p className="form__error" id="target-prompt-error">
              {fieldError("system_prompt")}
            </p>
          ) : null}
        </div>

        {error && Object.keys(error.fieldErrors).length === 0 ? (
          <p className="form__error" role="alert">
            {error.message}
          </p>
        ) : null}

        <button type="submit" disabled={submitting}>
          {submitting ? "Registering…" : "Register target"}
        </button>
      </form>
    </section>
  );
}
