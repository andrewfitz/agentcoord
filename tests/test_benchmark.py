"""Real service benchmark smoke and accepted-receipt oracle regressions."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

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
        "profile_observer":report.get("service_resources",{}).get("profile_observer"),
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
    profile = [json.loads(line) for line in Path(report["service_profile_path"]).read_text().splitlines()]
    observer = report["service_resources"]["profile_observer"]
    assert observer["ok"]
    assert len(profile) == observer["recorded_rows"] == sum(report["service_resources"]["calls"].values())
    assert Counter(row["phase"] for row in profile) == report["service_resources"]["calls"]
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


def test_profile_buffer_concurrent_calls_are_exact_without_dispatch_file_writes(tmp_path,monkeypatch):
    benchmark = benchmark_module()
    phase = tmp_path/"phase"
    phase.write_text("mixed")
    profile = tmp_path/"profile.jsonl"
    observer = benchmark.ProfileObserver(phase,128)
    original_open = Path.open
    flushing = False
    opened = []
    def observe_open(path,*args,**kwargs):
        if path in (profile,profile.with_suffix(".buffer.tmp")):
            assert flushing, "Profile I/O occupied a dispatch slot"
            opened.append(path)
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,"open",observe_open)
    barrier = threading.Barrier(8)
    def invoke(number):
        response = {"ok":True,"data":{"receipt":number}}
        barrier.wait(timeout=10)
        assert observer.invoke(lambda *_:response,None,SimpleNamespace(operation=f"operation-{number}")) is response
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(invoke,range(64)))
    assert not opened
    assert not profile.exists()
    flushing = True
    observer.flush(profile)
    rows = [json.loads(line) for line in profile.read_text().splitlines()]
    assert len(rows) == 64
    assert {row["operation"] for row in rows} == {f"operation-{number}" for number in range(64)}
    assert Counter(row["phase"] for row in rows) == {"mixed":64}
    assert observer.snapshot() == {"capacity":128,"recorded_rows":64,"calls":{"mixed":64},
        "dropped_rows":0,"flushed":True,"flush_error":None,"ok":True}


def test_profile_exhaustion_fails_evidence_without_changing_success(tmp_path):
    benchmark = benchmark_module()
    phase = tmp_path/"phase"
    phase.write_text("stress")
    observer = benchmark.ProfileObserver(phase,2)
    response = {"ok":True,"data":{"committed":True}}
    for number in range(3):
        assert observer.invoke(lambda *_:response,None,SimpleNamespace(operation=str(number))) is response
    observer.flush(tmp_path/"profile.jsonl")
    state = observer.snapshot(final=True)
    assert not state["ok"]
    assert state["dropped_rows"] == 1
    assert state["calls"] == {"stress":3}
    assert [row["operation"] for row in state["retained_rows"]] == ["0","1"]


def test_profile_flush_failure_retains_exact_bounded_evidence(tmp_path):
    benchmark = benchmark_module()
    phase = tmp_path/"phase"
    phase.write_text("stress")
    observer = benchmark.ProfileObserver(phase,2)
    response = {"ok":True,"data":{"committed":True}}
    assert observer.invoke(lambda *_:response,None,SimpleNamespace(operation="decision.request")) is response
    blocked = tmp_path/"blocked"
    blocked.write_text("A regular file cannot contain the profile")
    observer.flush(blocked/"profile.jsonl")
    state = observer.snapshot(final=True)
    assert not state["ok"]
    assert not state["flushed"]
    assert state["flush_error"]["type"] == "NotADirectoryError"
    assert state["calls"] == {"stress":1}
    assert state["retained_rows"][0]["operation"] == "decision.request"


def test_profile_phase_switch_is_read_by_the_next_invocation(tmp_path):
    benchmark = benchmark_module()
    phase = tmp_path/"phase"
    phase.write_text("mixed")
    observer = benchmark.ProfileObserver(phase,2)
    for name in ("mixed","stress"):
        phase.write_text(name)
        observer.invoke(lambda *_:{"ok":True},None,SimpleNamespace(operation="message.sync"))
    profile = tmp_path/"profile.jsonl"
    observer.flush(profile)
    assert [json.loads(line)["phase"] for line in profile.read_text().splitlines()] == ["mixed","stress"]
    assert observer.snapshot()["ok"]


def test_profile_late_handler_outcome_is_retained_after_join(tmp_path):
    benchmark = benchmark_module()
    phase = tmp_path/"phase"
    phase.write_text("stress")
    observer = benchmark.ProfileObserver(phase,2)
    entered,release = threading.Event(),threading.Event()
    response = {"ok":True,"data":{"committed":True}}
    outcomes = []
    def execute(*_):
        entered.set()
        assert release.wait(10)
        return response
    thread = threading.Thread(target=lambda:outcomes.append(observer.invoke(
        execute,None,SimpleNamespace(operation="message.send"))))
    thread.start()
    try:
        assert entered.wait(10)
        assert observer.snapshot()["recorded_rows"] == 0
        phase.write_text("overload")
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert outcomes == [response]
    profile = tmp_path/"profile.jsonl"
    observer.flush(profile)
    rows = [json.loads(line) for line in profile.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["phase"] == "stress"
    assert rows[0]["operation"] == "message.send"
    assert observer.snapshot()["ok"]
