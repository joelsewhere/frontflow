"""
publish_article — the Airflow DAG the "Publish an article" demo
workflow drives.

Drop this into your Airflow instance's `dags/` folder. It is written
for Airflow 3.1+ because it uses a human-in-the-loop task; the HITL
operator import and signature below may need a small adjustment for the
exact `apache-airflow-providers-standard` version you run.

The pipeline:

  build_content   render the article (reads the `headline` from the
                  triggering form's `conf`)
  editor_review   a branching human-in-the-loop gate — the editor picks
                  Approve / Request changes / Reject; the DAG routes
                  accordingly and the form renders this prompt
  publish         publish the approved article and push its URL to
                  XCom, where the form's XComPull operator collects it
  skip_publish    the not-approved path — nothing is published

The form builder triggers this DAG, polls `build_content`, responds to
`editor_review`, and on the Approve path waits for the run to finish
and pulls the `publish` return value.
"""

from __future__ import annotations

import time

from airflow.sdk import Param, dag, task

# Human-in-the-loop lives in the standard provider (Airflow 3.1+).
from airflow.providers.standard.operators.hitl import HITLBranchOperator


@dag(
    dag_id="publish_article",
    schedule=None,  # triggered by the form, never on a schedule
    catchup=False,
    tags=["form-builder", "demo"],
)
def publish_article():

    @task
    def build_content(**context) -> dict:
        """Render the article. Reads the headline the form passed in the
        DAG run `conf`."""
        conf = context["dag_run"].conf or {}
        headline = conf.get("headline", "Untitled")
        channel = conf.get("channel", "Blog")
        time.sleep(2)  # stand in for a real content build
        return {
            "headline": headline,
            "channel": channel,
            "word_count": 820,
        }

    # The human-in-the-loop gate — a branching review. The editor picks
    # one of three options; the DAG routes accordingly (publish runs
    # only on Approve), and the form's AirflowHitlBranch routes its own
    # chain on the same choice.
    editor_review = HITLBranchOperator(
        task_id="editor_review",
        subject="Review this article for publishing",
        body=(
            "Approve to publish, request changes to send it back for "
            "edits, or reject to decline it."
        ),
        options=["Approve", "Request changes", "Reject"],
        options_mapping={
            "Approve": "publish",
            "Request changes": "skip_publish",
            "Reject": "skip_publish",
        },
        defaults=["Approve"],
        multiple=False,
        params={"editor_note": Param("", type="string")},
    )

    @task
    def publish(content: dict, **context) -> str:
        """Publish the approved article. Its return value is pushed to
        XCom under `return_value` — what the form's XComPull collects."""
        slug = content["headline"].lower().replace(" ", "-")
        return (
            f"Published to the {content['channel']} channel at "
            f"https://example.com/{slug} "
            f"({content['word_count']} words)."
        )

    @task
    def skip_publish() -> str:
        """The not-approved path — nothing is published."""
        return "Article was not approved; nothing published."

    content = build_content()
    content >> editor_review >> [publish(content), skip_publish()]


publish_article()
