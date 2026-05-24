"""
A node with several `@backend` calls in sequence, then an Airflow
`TriggerDag` and a `TaskSensor` polling for completion.

What this demonstrates:

  - **Multi-backend chains.** The `process` node runs three
    `@backend` functions in declared order — `normalize`, `enrich`,
    `persist`. Each one's return appears as a separate row on the
    submission detail page (per-producer output, not a single
    `returned` value).
  - **Mock Airflow.** `TriggerDag` and `TaskSensor` both use
    `connection="mock"`, so the chain runs end-to-end with no real
    Airflow instance. The mock progression takes ~10s to walk
    queued → running → success.
  - **Operator-level poll rates.** The sensor declares
    `poll_interval_ms=5000` — slower than the framework's 2s
    default. The frontend honors per-operator rates: with this
    sensor in flight, the page refreshes every 5s instead of 2s.
    Suitable for long-running tasks where 2s polling is wasted
    traffic.
  - **Step references in operator config.** The sensor's `run_id`
    is `"{{ steps.trigger_pipeline.run_id }}"` — the run id the
    upstream `TriggerDag` produces, threaded into the sensor's
    config. Editing the trigger step re-runs the sensor (the
    operator-config dep cascade).
"""

from frontflow import Button, backend, displays, form, inputs, node, page
from frontflow.dsl.external import TaskSensor, TriggerDag


@form(
    form_id="multi_backend_pipeline",
    title="Multi-backend pipeline",
    tags=["airflow", "backend", "multi-backend"],
)
def multi_backend_pipeline_workflow():

    @page
    def intake():
        job_name = inputs.Text(
            label="Job name",
            required=True,
            placeholder="e.g. nightly-export",
        )
        priority = inputs.Integer(
            label="Priority (1–10)",
            required=True,
        )
        region = inputs.Radio(
            label="Region",
            options=["us-east", "us-west", "eu-west"],
            required=True,
        )
        return (
            displays.Markdown(
                "## New job\n\n"
                "Submit a job — the backend will normalize, enrich, "
                "persist it, then trigger a downstream DAG run."
            ),
            job_name,
            priority,
            region,
            Button("Submit job"),
        )

    @node
    def process():
        # A small confirmation step. The local inputs here feed
        # this node's @backend chain — node-internal backends take
        # local input variables as arguments, not cross-node refs.
        comment = inputs.TextBlock(
            label="Notes for this run (optional)",
            required=False,
        )
        tags = inputs.Text(
            label="Tags (comma-separated)",
            required=False,
            placeholder="ops, nightly, retry",
        )

        @backend
        def normalize(comment, tags):
            return {
                "comment": (comment or "").strip(),
                "tags": [
                    t.strip() for t in (tags or "").split(",") if t.strip()
                ],
                "normalized": True,
            }

        @backend
        def enrich(comment, tags):
            tag_list = [
                t.strip() for t in (tags or "").split(",") if t.strip()
            ]
            return {
                "tag_count": len(tag_list),
                "has_comment": bool((comment or "").strip()),
                "priority_tag": "ops" if "ops" in tag_list else "default",
            }

        @backend
        def persist(comment, tags):
            tag_list = [
                t.strip() for t in (tags or "").split(",") if t.strip()
            ]
            return {
                "record_id": f"run_{abs(hash((comment, tuple(tag_list)))) % 100000:05d}",
                "stored": True,
            }

        submit = Button("Run pipeline")
        # Three backends fire in sequence on submit. Each one's
        # return shows up as a separate row on the submission
        # detail page.
        submit >> normalize(comment, tags)
        submit >> enrich(comment, tags)
        submit >> persist(comment, tags)

        # Trigger a DAG using the mock connection — no real Airflow
        # is needed. The mock progresses queued → running → success
        # over ~10 seconds.
        submit >> TriggerDag(
            id="trigger_pipeline",
            dag_id="pipeline_dag",
            connection="mock",
        )
        # Wait for the DAG's main task. poll_interval_ms=5000 is
        # slower than the framework default (2s) — appropriate for
        # a multi-minute task where 2s polling is overkill.
        submit >> TaskSensor(
            id="wait_for_pipeline",
            dag_id="pipeline_dag",
            task_id="run_pipeline",
            run_id="{{ steps.trigger_pipeline.run_id }}",
            connection="mock",
            poll_interval_ms=5000,
            waiting_message="Running pipeline…",
        )

        return (
            displays.Markdown(
                "## Process\n\n"
                "Add any notes or tags, then run. Backends fire in "
                "sequence; the DAG triggers afterward."
            ),
            comment,
            tags,
            submit,
        )

    @node
    def done():
        return (
            displays.Markdown(
                "## All done\n\n"
                "Pipeline completed."
            ),
        )

    intake() >> process() >> done()


multi_backend_pipeline_workflow()
