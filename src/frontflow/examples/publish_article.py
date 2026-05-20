"""
Demo 1 — Article publishing request.

Showcases the Airflow integration end to end. The form collects an
article and its publishing options, then a chain of Airflow operators
trailing the submit button does the real orchestration:

  TriggerDag        — start a DAG run, passing the article as `conf`
  AirflowTaskSensor — poll the DAG's content-build task
  AirflowHitl       — pause for a human editor to approve the article
  AirflowDagSensor  — wait for the whole run to finish
  XComPull          — pull the published URL out of the DAG

A second node then displays the result. Every operator names the
`prod_airflow` connection from the connection store; the matching DAG
is `airflow_dags/publish_article_dag.py`.

The trigger produces a run id, referenced downstream as
`steps.<trigger id>.run_id`; the XCom pull's value is referenced as
`steps.<pull id>.value`.
"""

from frontflow import (
    AirflowDagSensor,
    AirflowHitlBranch,
    AirflowTaskSensor,
    Button,
    TriggerDag,
    XComPull,
    backend,
    displays,
    form,
    inputs,
    node,
)

# The connection-store entry every operator authenticates through.
CONNECTION = "prod_airflow"
# The Airflow DAG this workflow drives (see airflow_dags/).
DAG_ID = "publish_article"


@form(
    title="Publish an article",
    description=(
        "Submit an article and we'll run it through the publishing "
        "pipeline. An editor approves it before it goes live."
    ),
    workflow_id="publish_article",
    submission_id="{{ steps.draft.headline | slugify }}",
)
def publish_article_workflow():

    @node
    def draft():
        headline = inputs.Text(
            input_id="headline",
            label="Headline",
            required=True,
            placeholder="e.g. Quarterly product update",
        )
        body = inputs.TextBlock(
            label="Article body",
            required=True,
            placeholder="Write or paste the article here…",
        )
        channel = inputs.Radio(
            label="Channel",
            options=["Blog", "Newsletter", "Press release"],
            required=True,
        )
        feature = inputs.Checkbox(
            label="Feature on the homepage",
        )
        submit = Button("Send to pipeline ->")

        @backend
        def kickoff(headline, body, channel, feature):
            # Light normalization before the DAG run; the real work
            # happens in Airflow.
            return {"headline": (headline or "").strip()}

        # The Airflow operator chain — runs after the @backend, in order.
        # Each operator names an explicit `id` so downstream references —
        # `steps.<id>.run_id`, `steps.<id>.value` — are predictable.
        trigger = TriggerDag(
            id="trigger",
            connection=CONNECTION,
            dag_id=DAG_ID,
            conf={
                "headline": "{{ steps.draft.headline }}",
                "channel": "{{ steps.draft.channel }}",
            },
        )
        build = AirflowTaskSensor(
            connection=CONNECTION,
            dag_id=DAG_ID,
            task_id="build_content",
            run_id="{{ steps.trigger.run_id }}",
        )
        # The editor's human-in-the-loop review — and the branch point.
        # The option the editor picks routes the *form* to a different
        # node: Approve → published, Request changes → changes_requested,
        # Reject → rejected. The DAG's HITL task offers the same three
        # options. The form reads the choice and routes; the downstream
        # publish/pull operators live on the `published` node, since
        # they only run when the article is actually approved.
        review = AirflowHitlBranch(
            id="editor_review",
            connection=CONNECTION,
            dag_id=DAG_ID,
            task_id="editor_review",
            run_id="{{ steps.trigger.run_id }}",
            routes={
                "Approve": "published",
                "Request changes": "changes_requested",
                "Reject": "rejected",
            },
        )

        submit >> kickoff(
            headline, body, channel, feature
        ) >> trigger >> build >> review

        return displays.Column(
            headline,
            body,
            channel,
            feature,
            submit,
        )

    @node
    def published():
        # The Approve path. The editor approved, so the DAG goes on to
        # publish. This node's button runs the trailing Airflow chain —
        # wait for the run to finish, then pull the published URL.
        fetch_result = Button("Get the published link ->")
        finished = AirflowDagSensor(
            connection=CONNECTION,
            dag_id=DAG_ID,
            run_id="{{ steps.trigger.run_id }}",
        )
        pull_url = XComPull(
            id="pull_url",
            connection=CONNECTION,
            dag_id=DAG_ID,
            task_id="publish",
            run_id="{{ steps.trigger.run_id }}",
            key="return_value",
        )
        fetch_result >> finished >> pull_url

        return displays.Column(
            displays.Markdown("## Approved — publishing"),
            displays.Markdown(
                "The editor approved the article. Fetch the published "
                "link once the pipeline finishes."
            ),
            fetch_result,
        )

    @node
    def article_live():
        # Buttonless completion — shows the URL the XComPull surfaced.
        return displays.Column(
            displays.Markdown("## Your article is live"),
            displays.Markdown("{{ steps.pull_url.value }}"),
            displays.Markdown(
                "_Built by the Airflow pipeline and approved by an "
                "editor._"
            ),
        )

    @node
    def changes_requested():
        # The Request-changes path — the editor wants edits before
        # this can publish.
        return displays.Column(
            displays.Markdown("## Changes requested"),
            displays.Markdown(
                "The editor reviewed the article and asked for "
                "revisions before it can be published. Update the "
                "draft and submit it again."
            ),
        )

    @node
    def rejected():
        # The Reject path — the article will not be published.
        return displays.Column(
            displays.Markdown("## Not approved"),
            displays.Markdown(
                "The editor reviewed the article and decided not to "
                "publish it."
            ),
        )

    # The branch routes to one of three nodes; the Approve path then
    # continues `published >> article_live`. `draft` is called first
    # so it registers as the workflow's entry.
    draft_ref = draft()
    published_node = published()
    draft_ref >> [published_node, changes_requested(), rejected()]
    published_node >> article_live()


publish_article_workflow()
