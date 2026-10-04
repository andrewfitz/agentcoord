"""Real service benchmark smoke and accepted-receipt oracle regressions."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


def benchmark_module():
    path = Path(__file__).resolve().parents[1]/"benchmarks/workload.py"
    spec = importlib.util.spec_from_file_location("agentcoord_benchmark",path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_uses_real_distinct_sockets_and_exact_receipts(tmp_path):
    benchmark = benchmark_module()
    report = benchmark.workload(output=tmp_path/"report.json",clients=4,operations_per_client=10,
        warm_operations=12,inactive_actors=12,history_messages=24,smoke=True)
    stress = report["phases"].get("stress",{})
    overload = report["phases"].get("overload",{})
    assert report["ok"], {
        "exceptions":report["errors"], "receipt":report.get("receipt_oracle"),
        "phase_errors":{name:{key:phase.get(key) for key in ("errors","worker_errors")}
                        for name,phase in report["phases"].items()},
        "history_errors":stress.get("history_errors"),
        "slow_failures":[row for row in stress.get("slow_results",[]) if row["state"]!="succeeded"],
        "job_failures":[row for row in stress.get("job_results",[]) if row["state"]!="succeeded"],
        "overload_rejected":overload.get("rejected"),
        "overload_details":overload.get("unexpected_error_details"),
        "warm_effects":report.get("service_resources",{}).get("effects",{}).get("warm"),
        "handler_errors":report.get("service_resources",{}).get("handler_errors"),
    }
    assert report["connected_socket_clients"] == 4
    assert report["phases"]["warm"]["operations"] == 12
    for phase in ("mixed","stress"):
        assert report["phases"][phase]["barrier_clients"] == 4
        assert report["phases"][phase]["operations"] == 40
    assert report["receipt_oracle"]["messages"] == 21
    assert report["receipt_oracle"]["decisions"] == 16
    assert report["receipt_oracle"]["defects"] == []
    assert report["service_resources"]["handler_errors"] == []
    assert not report["acceptance"]["complete_workload"]
    assert not report["acceptance_passed"]


@pytest.mark.parametrize("corruption",["missing_receipt","changed_payload","wrong_recipient"])
def test_oracle_rejects_lost_or_different_accepted_data(tmp_path,corruption):
    from agentcoord.application import build_service
    from agentcoord.config import register_workspace
    from agentcoord.core import Call
    from agentcoord.identity import bind_native
    benchmark = benchmark_module()
    tmp_path = tmp_path.resolve()
    root = tmp_path/"repo"
    root.mkdir()
    subprocess.run(["git","init","--quiet"],cwd=root,check=True)
    service = build_service(register_workspace(root,state_root=tmp_path/"state"))
    actors = benchmark.seed(service,clients=2,inactive_actors=1,history_messages=1)
    sender,recipient = actors
    context = bind_native(service.store,sender["native"])["context"]
    arguments = {"recipients":[recipient["id"]],"kind":"FINDING","subject":"Synthetic message","body":"Exact body"}
    response = service.execute(context,Call("message.send",arguments,"original-key"))
    assert response["ok"], response
    accepted = [{"workspace_id":service.workspace.id,"actor_id":sender["id"],"operation":"message.send",
                 "arguments":dict(arguments),"key":"original-key","result":response["data"],
                 "context":{"task_generation":context.task_generation,"execution_generation":context.execution_generation}}]
    with service.store.read() as tx:
        assert benchmark.assert_receipts(tx.connection,accepted)["ok"]
    if corruption == "changed_payload":
        accepted[0]["arguments"]["body"] = "Different body"
    else:
        with service.store.write() as tx:
            if corruption == "missing_receipt":
                tx.connection.execute("DELETE FROM idempotency WHERE actor_id=? AND retry_key=?",(sender["id"],"original-key"))
            else:
                tx.connection.execute("DELETE FROM recipients WHERE message_id=?",(response["data"]["id"],))
    with service.store.read() as tx:
        result = benchmark.assert_receipts(tx.connection,accepted)
    assert not result["ok"]
    assert result["defects"]
