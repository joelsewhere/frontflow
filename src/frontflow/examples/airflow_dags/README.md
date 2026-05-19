# Airflow DAGs

The DAGs the demo workflows drive. Each pairs with a workflow in
`backend/workflows_user/`.

| DAG file | Workflow | Demonstrates |
|----------|----------|--------------|
| `publish_article_dag.py` | `publish_article.py` | Trigger, task/DAG sensors, HITL, XCom pull |

## Setup

1. **Airflow version** — these DAGs use a human-in-the-loop task, which
   requires **Airflow 3.1+** and `apache-airflow-providers-standard`.
2. **Install the DAGs** — copy the `*_dag.py` files into your Airflow
   instance's `dags/` folder.
3. **Add a connection** — in the form builder's console, open
   **Connections** and add an `airflow` connection named `prod_airflow`
   (the name the demo workflows reference) pointing at your instance's
   base URL, with credentials for its REST API.
4. **Reachability** — the form builder backend must be able to reach
   the Airflow base URL over HTTP.

## Notes

- The integration targets the Airflow 3.x `/api/v2` REST API and the
  HITL operators from `apache-airflow-providers-standard` (Airflow 3.1+).
- A workflow whose operators name a connection that isn't configured
  falls back to mock progression, so the forms still run end to end
  before Airflow is wired up.
