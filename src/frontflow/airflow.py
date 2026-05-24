"""Airflow operator namespace — `from frontflow import airflow`.

Connected-Airflow operators are surfaced under a namespace so a workflow
file reads naturally:

    from frontflow import airflow

    @node
    def kick_off():
        run = airflow.TriggerDag(dag_id="etl", run_id="{{ steps.start.ref }}")
        wait = airflow.TaskSensor(dag_id="etl", task_id="load",
                                  run_id="{{ steps.kick_off.run.run_id }}")
        return run, wait, Button()

The seven operators exposed here are the ones a workflow author calls.
The internal helper types (`ExternalTask`, `AirflowOperator`,
`AirflowStatus`) remain importable from the package root for tooling
and type inspection — they are not operators, just the type lineage.
"""

from frontflow.dsl.external import (
    TriggerDag,
    TaskSensor,
    TaskStateSensor,
    DagSensor,
    XComPull,
    Hitl,
    HitlBranch,
    HitlResponse,
)

__all__ = [
    "TriggerDag",
    "TaskSensor",
    "TaskStateSensor",
    "DagSensor",
    "XComPull",
    "Hitl",
    "HitlBranch",
    "HitlResponse",
]
