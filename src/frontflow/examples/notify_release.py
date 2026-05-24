"""
Variables tool — install-scoped config referenced from a workflow.

This form posts a release announcement to a notification pipeline. It
exists to demonstrate the two ways a workflow author can reach into the
Variables store:

  1. **Python helper at workflow load.**

         from frontflow import variables
         DEFAULT_CHANNEL = variables.get("release_channel", default="#general")

     Use this for constants that should be resolved once when the
     workflow file compiles — environment names, default channels,
     DAG ids selected by region. Without a `default=`, a missing
     variable raises `MissingVariableError` so the workflow fails
     to load and the problem surfaces immediately in /api/forms.

  2. **Template-resolved at runtime.**

         TriggerDag(
             dag_id="{{ variables.release_dag_id }}",
             conf={"webhook": "{{ variables.release_webhook_url }}"},
             connection="mock",
         )

     Use this for values that thread through to operator config.
     Missing references render as empty string (the same non-strict
     behavior as `steps.x.y` lookups), so a typo lives until the
     operator runs — which then fails with a clear error.

Variables the form looks for (set them in the Variables admin page):

  - `release_channel`         (Python helper, has default)  →  e.g. "#releases"
  - `release_dag_id`          (template, required for real Airflow)  →  e.g. "notify_release_dag"
  - `release_webhook_url`     (template)                             →  e.g. "https://hooks.example.com/T123"

The bundled Airflow DAG (`airflow_dags/notify_release_dag.py`) is
referenced via the `release_dag_id` variable — the value at install
time decides which DAG the trigger lands on, so the same workflow
file can target different Airflow environments without editing.

The connection is "mock" so the form runs end-to-end with no real
Airflow needed; swap to a real connection name once you have one.
"""

from frontflow import (
    Button,
    backend,
    displays,
    form,
    inputs,
    node,
    variables,
)
from frontflow.dsl.external import TriggerDag


# --- Boot-time variables ---------------------------------------------------
#
# Resolved when this file is exec'd during a workflow scan. Using
# `default=` so the form still loads when the variable is unset — a
# fresh install sees "#general" until an admin sets `release_channel`
# in the Variables page.

DEFAULT_CHANNEL = variables.get("release_channel", default="#general")


# --- Form ------------------------------------------------------------------


@form(
    form_id="notify_release",
    title="Announce a release",
    description="Posts a release note to the team's notification pipeline.",
    tags=["variables", "airflow", "templating"],
)
def notify_release_workflow():

    @node
    def compose():
        version = inputs.Text(
            id="version",
            label="Version",
            required=True,
            placeholder="e.g. 2.4.0",
        )
        summary = inputs.TextBlock(
            id="summary",
            label="Release summary",
            required=True,
            placeholder="What's in this release?",
        )
        # The channel input pre-fills from the install-default —
        # the boot-time variable above. End user can override per
        # submission.
        channel = inputs.Text(
            id="channel",
            label="Channel",
            default=DEFAULT_CHANNEL,
            help=(
                f"Defaults to {DEFAULT_CHANNEL!r} from the "
                "`release_channel` variable. Edit to override."
            ),
        )

        submit = Button("Send release note")

        # Trigger the notification DAG. Both fields are templated:
        # `dag_id` from `release_dag_id` (install-scoped), `webhook`
        # from `release_webhook_url`. On a fresh install these
        # variables aren't set — the operator's URL resolves to
        # empty and the trigger fails. Set them in the Variables
        # admin page (or use the "mock" connection below, which
        # ignores the dag_id and webhook anyway).
        submit >> TriggerDag(
            id="trigger_notify",
            dag_id="{{ variables.release_dag_id }}",
            conf={
                "version": "{{ steps.compose.version }}",
                "summary": "{{ steps.compose.summary }}",
                "channel": "{{ steps.compose.channel }}",
                "webhook": "{{ variables.release_webhook_url }}",
            },
            connection="mock",
        )

        return (
            displays.Markdown(
                "## Release announcement\n\n"
                "Fill in the version and summary; the workflow posts "
                "it to the channel below. The DAG id and webhook URL "
                "come from install Variables — see the file's docstring."
            ),
            version,
            summary,
            channel,
            submit,
        )

    @node
    def done():
        return (
            displays.Markdown(
                "## Sent\n\n"
                "The release notification has been queued."
            ),
        )

    compose() >> done()


notify_release_workflow()
