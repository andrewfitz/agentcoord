"""Measure an isolated real agentcoord service; never discover a live workspace.

Install the package first, then run this file with --output REPORT.json. --smoke
uses small fixtures and cannot certify the full acceptance workload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import resource
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path

BODY = "Synthetic historical body"
MEASURED_BODY = "Measured fixture message"


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"n": 0}
    return {"n": len(ordered), "p50": ordered[math.ceil(len(ordered) * .5)-1],
            "p95": ordered[math.ceil(len(ordered) * .95)-1], "max": ordered[-1],
            "mean": sum(ordered)/len(ordered)}


def process_usage():
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    scale = 1 if sys.platform == "darwin" else 1024
    return {"cpu_seconds": own.ru_utime + own.ru_stime,
            "children_cpu_seconds": children.ru_utime + children.ru_stime,
            "peak_rss_bytes": own.ru_maxrss * scale,
            "children_peak_rss_bytes": children.ru_maxrss * scale,
            "open_file_limit": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
            "load_average": list(os.getloadavg())}


def assert_receipts(connection, accepted):
    """Check exact accepted mutation ownership/payload/result and domain content."""
    defects = []
    seen = {}
    message_ids = set()
    decision_ids = set()
    for item in accepted:
        pair = (item["actor_id"], item["key"])
        if pair in seen:
            if item != seen[pair]:
                defects.append({"key":item["key"],"reason":"retry changed an accepted logical operation"})
            continue
        seen[pair] = item
        row = connection.execute("SELECT operation,input_sha256,result_json,payload_sha256,context_json FROM idempotency WHERE actor_id=? AND retry_key=?", pair).fetchone()
        payload = {"workspace":item["workspace_id"], "actor":item["actor_id"],
                   "operation":item["operation"], "arguments":item["arguments"]}
        expected_payload = hashlib.sha256(json.dumps(payload, sort_keys=True,
            separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        guards = item["context"]
        expected_hash = hashlib.sha256(json.dumps({**payload,
            "expected_task_generation":guards.get("task_generation"),
            "expected_execution_generation":guards.get("execution_generation")}, sort_keys=True,
            separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        if row is None:
            defects.append({"key":item["key"],"reason":"accepted receipt absent"})
            continue
        operation, digest, result_json, payload_digest, context_json = row
        result = json.loads(result_json)
        if (operation != item["operation"] or digest != expected_hash or payload_digest != expected_payload
                or json.loads(context_json) != guards or result != item["result"]):
            defects.append({"key":item["key"],"reason":"receipt ownership/payload/result differs"})
        if operation == "message.send":
            identifier = result["id"]
            if identifier in message_ids:
                defects.append({"key":item["key"],"reason":"different logical sends share a message"})
            message_ids.add(identifier)
            message = connection.execute("SELECT sender_id,body_utf8,body_sha256,subject,thread,kind FROM messages WHERE id=?",(identifier,)).fetchone()
            args = item["arguments"]
            raw = args["body"].encode()
            wanted = (item["actor_id"],raw,hashlib.sha256(raw).hexdigest(),args["subject"],args.get("thread",""),args["kind"])
            if message is None or tuple(message) != wanted:
                defects.append({"key":item["key"],"reason":"message content differs"})
            recipients = {row[0] for row in connection.execute("SELECT actor_id FROM recipients WHERE message_id=?",(identifier,))}
            if recipients != set(args["recipients"]):
                defects.append({"key":item["key"],"reason":"message recipients differ"})
        if operation == "decision.request":
            identifier = result["id"]
            if identifier in decision_ids:
                defects.append({"key":item["key"],"reason":"different logical decisions share a decision"})
            decision_ids.add(identifier)
            decision = connection.execute("SELECT sender_id,recipient_id,subject,body FROM decisions WHERE id=?",(identifier,)).fetchone()
            args = item["arguments"]
            if decision is None or tuple(decision) != (item["actor_id"],args["recipient"],args["subject"],args["body"]):
                defects.append({"key":item["key"],"reason":"decision ownership/content differs"})
        if operation == "decision.resolve":
            decision = connection.execute("SELECT state,response FROM decisions WHERE id=?",(item["arguments"]["id"],)).fetchone()
            if decision is None or tuple(decision) != (item["arguments"]["state"],item["arguments"]["response"]):
                defects.append({"key":item["key"],"reason":"decision resolution differs"})
    foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    if foreign_keys:
        defects.append({"reason":"foreign key violations","records":foreign_keys})
    return {"accepted_unique_keys":len(seen),"messages":len(message_ids),"decisions":len(decision_ids),
            "defects":defects,"ok":not defects}


def timed_call(client, operation, arguments, key, *, retries=4):
    """Retry only explicit bounded admission errors, retaining the same key."""
    from agentcoord.transport import encode_frame
    start = time.perf_counter()
    rejected = []
    for attempt in range(retries+1):
        response = client.call(operation, arguments, key=key)
        if response.get("ok") or response.get("error",{}).get("code") != "SERVICE_BUSY":
            break
        rejected.append(response["error"])
        if attempt < retries:
            time.sleep(min(.005 * 2**attempt,.04))
    return response, {"operation":operation,"client_ms":(time.perf_counter()-start)*1000,
                      "response_bytes":len(encode_frame(response)),"rejected_admissions":len(rejected),
                      "ok":response.get("ok") is True,"error":response.get("error")}


def run_clients(clients, work, *, timeout=120):
    """All distinct connected socket clients wait at a barrier before work."""
    barrier = threading.Barrier(len(clients)+1, timeout=timeout)
    errors = []
    lock = threading.Lock()
    def worker(index, client):
        try:
            barrier.wait()
            work(index, client)
        except Exception as error:  # noqa: BLE001 - Thread failures are collected and fail the workload.
            with lock:
                errors.append({"client":index,"exception":type(error).__name__,"message":str(error)[:1000]})
    threads = [threading.Thread(target=worker,args=(i,client),name=f"benchmark-client-{i}")
               for i,client in enumerate(clients)]
    for thread in threads:
        thread.start()
    started = time.perf_counter()
    barrier.wait()
    deadline = started+timeout
    for thread in threads:
        thread.join(max(0,deadline-time.perf_counter()))
    alive = [thread.name for thread in threads if thread.is_alive()]
    if alive:
        for client in clients:
            if client._socket is not None:
                try:
                    client._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        for thread in threads:
            thread.join(5)
        raise RuntimeError("Client workload exceeded its deadline: "+", ".join(alive))
    return {"barrier_clients":len(clients),"threads":len(threads),
            "elapsed_seconds":time.perf_counter()-started,"worker_errors":errors}


def summarize(observations):
    return {"operations":len(observations),"client_ms":distribution([r["client_ms"] for r in observations]),
            "response_bytes":distribution([r["response_bytes"] for r in observations]),
            "accepted":sum(r["ok"] for r in observations),
            "rejected_admissions":sum(r["rejected_admissions"] for r in observations),
            "errors":dict(Counter((r.get("error") or {}).get("code","UNKNOWN") for r in observations if not r["ok"])),
            "per_operation":{name:distribution([r["client_ms"] for r in observations if r["operation"]==name])
                             for name in sorted({r["operation"] for r in observations})}}


def seed(service, *, clients, inactive_actors, history_messages):
    """Offline synthetic fixture preparation, excluded from all timing."""
    from agentcoord.core import Context
    from agentcoord.identity import register_native
    from agentcoord.messages import append_message
    active = []
    archived = []
    with service.store.write() as tx:
        for index in range(clients + inactive_actors):
            native = {"harness":"codex","native_session_id":f"synthetic-benchmark-{index}",
                      "label":f"Synthetic{index}","task":"synthetic-benchmark"}
            actor = register_native(tx,native)
            if index < clients:
                active.append({"native":native,"id":actor["id"],"generation":actor["current_task_generation"]})
            else:
                tx.connection.execute("UPDATE actors SET archived=1,reported_state='completed' WHERE id=?",(actor["id"],))
                archived.append(actor)
        if history_messages and not archived:
            raise ValueError("Historical fixture requires at least one archived actor")
        for start in range(0,history_messages,500):
            records = []
            recipients = []
            for index in range(start,min(start+500,history_messages)):
                sender = archived[index % len(archived)]
                recipient = archived[(index+1) % len(archived)]
                identifier = str(uuid.uuid4())
                sequence = tx.event("messages","historical",identifier,sender["id"],{})
                raw = BODY.encode()
                records.append((identifier,sender["id"],sender["current_task_generation"],"historical","FINDING",
                                "Synthetic history",raw,hashlib.sha256(raw).hexdigest(),len(raw),"{}",sequence,tx.now_us))
                recipients.append((identifier,recipient["id"],recipient["current_task_generation"],tx.now_us))
            tx.connection.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",records)
            tx.connection.executemany("INSERT INTO recipients(message_id,actor_id,recipient_task_generation,role,handled_us) VALUES (?,?,?,'to',?)",recipients)
        context = Context(service.workspace.id,active[0]["id"],task_generation=active[0]["generation"])
        for actor in active:
            actor["seed_message"] = append_message(tx,context,recipient_ids=[actor["id"]],kind="FINDING",
                subject="Synthetic seed",body=BODY,thread="seed")["id"]
        violations = tx.connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Seed has invalid relationships")
    return active


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(value,sort_keys=True,allow_nan=False)+"\n")
    temporary.replace(path)


class ProfileObserver:
    """Bound exact observations without adding profile-file I/O to dispatch."""

    def __init__(self, phase_path, capacity):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("Profile capacity must be a positive integer")
        self.phase_path = phase_path
        self.capacity = capacity
        self.lock = threading.Lock()
        self.rows = []
        self.calls = Counter()
        self.dropped_rows = 0
        self.flushed = False
        self.flush_error = None

    def invoke(self, execute, context, call):
        current = self.phase_path.read_text().strip()
        start = time.perf_counter()
        result = execute(context,call)
        row = {"phase":current,"operation":call.operation,"service_ms":(time.perf_counter()-start)*1000,
               "ok":result.get("ok") is True}
        with self.lock:
            self.calls[current] += 1
            if len(self.rows) < self.capacity:
                self.rows.append(row)
            else:
                self.dropped_rows += 1
        return result

    def flush(self, path):
        """Called only after the real factory closes and joins its handlers."""
        with self.lock:
            rows = list(self.rows)
        temporary = path.with_suffix(".buffer.tmp")
        try:
            with temporary.open("w") as output:
                for row in rows:
                    output.write(json.dumps(row)+"\n")
            temporary.replace(path)
        except OSError as error:
            self.flush_error = {"type":type(error).__name__,"message":str(error)[:2000]}
        else:
            self.flushed = True

    def snapshot(self, *, final=False):
        with self.lock:
            result = {"capacity":self.capacity,"recorded_rows":len(self.rows),"calls":dict(self.calls),
                      "dropped_rows":self.dropped_rows,"flushed":self.flushed,"flush_error":self.flush_error}
            result["ok"] = (self.flushed and not self.dropped_rows and self.flush_error is None
                            and sum(self.calls.values())==len(self.rows))
            if final and (self.dropped_rows or self.flush_error is not None):
                result["retained_rows"] = list(self.rows)
            return result


def server_process(spec_path):
    """Profile the real application factory in its own owned process."""
    import urllib.request

    from agentcoord import application
    from agentcoord.config import workspace_from_id
    spec = json.loads(spec_path.read_text())
    workspace = workspace_from_id(spec["workspace_id"],state_root=Path(spec["state_root"]))
    service = application.build_service(workspace)
    source_root = Path(application.__file__).resolve().parent
    source_hashes = {path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in source_root.glob("*.py")}
    profile = Path(spec["profile"])
    stats = Path(spec["stats"])
    phase_path = Path(spec["phase"])
    stopped = threading.Event()
    lock = threading.Lock()
    counters = Counter()
    observer = ProfileObserver(phase_path,spec["profile_capacity"])
    handler_errors = []
    children = []
    peak_live_children = 0
    execute = service.execute
    def phase():
        return phase_path.read_text().strip()
    def profiled(context, call):
        return observer.invoke(execute,context,call)
    service.execute = profiled
    popen = subprocess.Popen
    urlopen = urllib.request.urlopen
    def observed_popen(*arguments,**keywords):
        nonlocal peak_live_children
        child = popen(*arguments,**keywords)
        with lock:
            counters[(phase(),"subprocess")] += 1
            children[:] = [process for process in children if process.poll() is None]
            children.append(child)
            peak_live_children = max(peak_live_children,len(children))
        return child
    def observed_urlopen(*arguments,**keywords):
        with lock:
            counters[(phase(),"http")] += 1
        return urlopen(*arguments,**keywords)
    subprocess.Popen = observed_popen
    urllib.request.urlopen = observed_urlopen
    signal.signal(signal.SIGTERM,lambda *_:stopped.set())
    signal.signal(signal.SIGINT,lambda *_:stopped.set())
    with application.make_server(service) as server:
        handle_error = server.handle_error
        def observed_handle_error(request, client_address):
            error = sys.exception()
            with lock:
                handler_errors.append({"phase":phase(),"type":type(error).__name__,
                                       "message":str(error)[:2000]})
            handle_error(request,client_address)
        server.handle_error = observed_handle_error
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def snapshot(*, final=False):
            profile_state = observer.snapshot(final=final)
            call_counts = profile_state["calls"]
            with lock:
                counts = {key:{kind:counters[(key,kind)] for kind in ("subprocess","http")} for key in call_counts}
                observed_errors = list(handler_errors)
                live_children = sum(child.poll() is None for child in children)
            atomic_json(stats,{"pid":os.getpid(),"usage":process_usage(),"calls":call_counts,
                               "effects":counts,"health":server.health(),
                               "handler_errors":observed_errors,
                               "profile_observer":profile_state,
                               "source_sha256":source_hashes,
                               "live_child_processes":live_children,"peak_live_child_processes":peak_live_children,
                               "live_threads":threading.active_count()})
        try:
            while not stopped.wait(.25):
                snapshot()
        finally:
            server.shutdown()
            thread.join(10)
            snapshot()
    # Closing the real factory joins connection handlers; retain late unbind errors.
    observer.flush(profile)
    snapshot(final=True)
    return 0


def cold_client(spec_path):
    from agentcoord.transport import Client
    spec = json.loads(spec_path.read_text())
    with Client(Path(spec["socket"]),spec["native"],workspace_id=spec["workspace_id"]) as client:
        context = dict(client._next_context)
        print(json.dumps({"response":client.call(spec["operation"],spec["arguments"],key=spec["key"]),
                          "context":context},allow_nan=False))
    return 0


def soak(connected, actors, invoke, sample, *, seconds, rate):
    """Sustained real socket traffic; synthetic actors are not native evidence."""
    started = time.monotonic()
    deadline = started + seconds
    messages, decisions = deque(), deque()
    samples = [sample()]
    next_sample = started + 60
    steps = 0
    while time.monotonic() < deadline:
        owner = steps % len(connected)
        actor = actors[owner]
        key = f"soak-{steps}"
        choice = steps % 6
        if choice == 0:
            invoke(connected[owner], actor, "work.activity", {
                "paths": ["fixture.txt"], "note": f"Synthetic coherent milestone {steps}"}, key, "soak")
        elif choice == 1:
            recipient = (owner + 1) % len(connected)
            result = invoke(connected[owner], actor, "message.send", {
                "recipients": [actors[recipient]["id"]], "kind": "FINDING", "subject": "Soak fixture",
                "body": MEASURED_BODY, "paths": ["fixture.txt"]}, key, "soak")
            messages.append((recipient, result["id"]))
        elif choice == 2 and messages:
            recipient, message = messages.popleft()
            invoke(connected[recipient], actors[recipient], "message.consume", {"id": message}, key, "soak")
        elif choice == 3:
            result = invoke(connected[owner], actor, "decision.request", {
                "recipient": actors[(owner + 1) % len(connected)]["id"], "subject": "Soak dependency",
                "body": "Synthetic cancellation will settle this fixture", "paths": ["fixture.txt"]}, key, "soak")
            decisions.append((owner, result["id"]))
        elif choice == 4 and decisions:
            requester, decision = decisions.popleft()
            invoke(connected[requester], actors[requester], "decision.resolve", {
                "id": decision, "state": "cancelled", "response": "Synthetic cancellation"}, key, "soak")
        else:
            invoke(connected[owner], actor, "work.evidence", {"paths": ["fixture.txt"], "limit": 3}, None, "soak")
        steps += 1
        now = time.monotonic()
        if now >= next_sample:
            samples.append(sample())
            next_sample = now + 60
            print(json.dumps({"phase": "soak", "elapsed_seconds": round(now-started, 1),
                              "operations": steps, "sample": samples[-1]}), flush=True)
        time.sleep(max(0, min(deadline - now, started + steps / rate - now)))
    samples.append(sample())
    return {"elapsed_seconds": time.monotonic() - started, "target_rate": rate,
            "operations": steps, "samples": samples, "unsettled_fixture_messages": len(messages),
            "unsettled_fixture_decisions": len(decisions)}


def workload(*, output, clients=100, operations_per_client=10, warm_operations=1000,
             inactive_actors=10000, history_messages=100000, smoke=False, soak_seconds=0, soak_rate=20):
    from agentcoord import application
    from agentcoord.config import register_workspace
    from agentcoord.transport import Client
    if type(soak_seconds) is not int or not 0 <= soak_seconds <= 86400 or type(soak_rate) is not int or not 1 <= soak_rate <= 100:
        raise ValueError("Soak duration/rate must be bounded integers")
    output = Path(output).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    accepted = []
    all_observations = []
    phases = {}
    lock = threading.Lock()
    errors = []
    report = {"schema_version":1,"smoke":smoke,"host":{"platform":platform.platform(),"python":sys.version},
              "workload":{"clients":clients,"operations_per_client":operations_per_client,"warm_operations":warm_operations,
                          "inactive_actors":inactive_actors,"history_messages":history_messages,
                          "historical_body_bytes":len(BODY.encode()),"warm_send_body_bytes":len(MEASURED_BODY.encode()),
                          "soak_seconds":soak_seconds,"soak_rate":soak_rate},
              "limitations":["All actors/native session IDs are synthetic; this is not installed native-client/lifecycle evidence.",
                             "Seeding is offline and excluded from timing; historical recipient records are handled.",
                             "Service resource metrics include profiling/snapshot observer overhead; driver resources are separate.",
                             "RSS is a cumulative process high-water mark, not aggregate concurrent memory. Child counts cover direct Popen launches; Git hook descendants are not enumerated.",
                             "Shared host load is recorded, not controlled; old-system Mail fixture limits still apply.",
                             "Cold timing launches the package Client helper, not an installed CLI; startup boundaries differ from OLD.",
                             "The slow Git hook is a private synthetic fixed-duration load; job load uses real durable reminders."]}
    with tempfile.TemporaryDirectory(prefix="agentcoord-benchmark-",dir="/tmp") as temporary:
        scratch = Path(temporary).resolve()
        root = scratch/"repo"
        root.mkdir()
        for command in (["git","init","--quiet"],["git","config","user.name","Synthetic Benchmark"],
                        ["git","config","user.email","benchmark@example.invalid"]):
            subprocess.run(command,cwd=root,check=True,capture_output=True)
        for index in range(4):
            (root/f"slow-{index}.txt").write_text("Synthetic baseline\n")
        (root/"fixture.txt").write_text("synthetic benchmark content\n")
        subprocess.run(["git","add","slow-0.txt","slow-1.txt","slow-2.txt","slow-3.txt"],cwd=root,check=True,capture_output=True)
        subprocess.run(["git","commit","--quiet","-m","Synthetic baseline"],cwd=root,check=True,capture_output=True)
        workspace = register_workspace(root,state_root=scratch/"state")
        service = application.build_service(workspace)
        actor_fixture = seed(service,clients=clients,inactive_actors=inactive_actors,history_messages=history_messages)
        with service.store.read() as tx:
            report["fixture_counts"] = {"inactive_actors":tx.connection.execute("SELECT COUNT(*) FROM actors WHERE archived=1").fetchone()[0],
                "active_actors":tx.connection.execute("SELECT COUNT(*) FROM actors WHERE archived=0").fetchone()[0],
                "historical_messages":tx.connection.execute("SELECT COUNT(*) FROM messages WHERE thread='historical'").fetchone()[0]}
        report["configuration"] = {key:getattr(service.config,key) for key in
            ("fast_workers","fast_queue","slow_workers","slow_queue","frame_bytes","action_bytes")}
        phase_path = scratch/"phase"
        phase_path.write_text("startup")
        profile_path = scratch/"service-profile.jsonl"
        stats_path = scratch/"service-stats.json"
        spec_path = scratch/"server-spec.json"
        atomic_json(spec_path,{"workspace_id":workspace.id,"state_root":str(scratch/"state"),"phase":str(phase_path),
                              "profile":str(profile_path),"stats":str(stats_path),
                              "profile_capacity":warm_operations+(3 if smoke else 30)+clients*operations_per_client*2
                                  +min(4,clients)*3+4096+soak_seconds*soak_rate})
        source_root = Path(application.__file__).resolve().parent
        report["source_sha256"] = {path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in source_root.glob("*.py")}
        report["benchmark_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join((str(source_root.parent),environment.get("PYTHONPATH","")))
        daemon_log = output.with_suffix(".daemon.log")
        connected = []
        operator = None
        process = None
        driver_before = process_usage()
        def phase(name):
            temporary_marker = phase_path.with_suffix(".tmp")
            temporary_marker.write_text(name)
            temporary_marker.replace(phase_path)
        def call(client,actor,operation,arguments,key,phase_name):
            context = dict(client._next_context)
            response,observation = timed_call(client,operation,arguments,key)
            observation["phase"] = phase_name
            with lock:
                all_observations.append(observation)
                if response.get("ok") and key is not None:
                    accepted.append({"workspace_id":workspace.id,"actor_id":actor["id"],"operation":operation,
                                     "arguments":arguments,"key":key,"result":response["data"],"context":context})
            if not response.get("ok"):
                raise RuntimeError(f"{operation}: {response.get('error')}")
            return response["data"]
        def invoke_mix(index,client,phase_name):
            actor = actor_fixture[index]
            recipient = actor_fixture[(index+1)%clients]
            current_decision = None
            for number in range(operations_per_client):
                key = f"{phase_name}-{index}-{number}"
                choice = number%5
                if choice == 0:
                    call(client,actor,"message.send",{"recipients":[recipient["id"]],"kind":"FINDING",
                        "subject":"Synthetic message","body":BODY,"thread":"benchmark","paths":["slow-0.txt"]},key,phase_name)
                elif choice == 1:
                    call(client,actor,"message.get",{"id":actor["seed_message"]},None,phase_name)
                elif choice == 2:
                    call(client,actor,"message.sync",{},key,phase_name)
                elif choice == 3:
                    current_decision = call(client,actor,"decision.request",{"recipient":recipient["id"],
                        "subject":"Synthetic decision","body":BODY,"paths":["slow-0.txt"]},key,phase_name)
                else:
                    if current_decision is None:
                        raise RuntimeError("Missing accepted decision")
                    call(client,actor,"decision.resolve",{"id":current_decision["id"],"state":"cancelled",
                        "response":"Synthetic cancellation"},key,phase_name)
        try:
            with daemon_log.open("wb") as log:
                process = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"--server",str(spec_path)],
                                           env=environment,stdout=log,stderr=log,start_new_session=True)
                deadline = time.monotonic()+30
                while not stats_path.exists():
                    if process.poll() is not None:
                        raise RuntimeError(f"Service exited {process.returncode}; inspect {daemon_log}")
                    if time.monotonic()>deadline:
                        raise RuntimeError("Service startup exceeded30seconds")
                    time.sleep(.02)
                operator = Client(workspace.socket_path,workspace_id=workspace.id,operator=True,timeout=30).connect()
                connected = [Client(workspace.socket_path,item["native"],workspace_id=workspace.id,timeout=30).connect()
                             for item in actor_fixture]
                if len({id(client._socket) for client in connected}) != clients:
                    raise RuntimeError("Benchmark clients do not own distinct connected sockets")
                report["connected_socket_clients"] = len(connected)
                report["service_before_measurement"] = json.loads(stats_path.read_text())
                phase("warm")
                for number in range(warm_operations):
                    choice = number%3
                    actor_index = 1 if choice == 2 else 0
                    actor = actor_fixture[actor_index]
                    if choice == 0:
                        call(connected[0],actor,"message.sync",{"limit":5},f"warm-{number}","warm")
                    elif choice == 1:
                        call(connected[0],actor,"message.send",{"recipients":[actor_fixture[1]["id"]],"kind":"FINDING",
                            "subject":"Synthetic message","body":MEASURED_BODY,"thread":"baseline","paths":["fixture.txt"]},f"warm-{number}","warm")
                    else:
                        call(connected[actor_index],actor,"message.inbox",{"limit":5},None,"warm")
                phases["warm"] = summarize([r for r in all_observations if r["phase"]=="warm"])
                phase("cold")
                cold_count = 3 if smoke else 30
                client_spec = scratch/"client-spec.json"
                from agentcoord.transport import encode_frame
                for number in range(cold_count):
                    actor = actor_fixture[1 if number%3==2 else 0]
                    key = f"cold-{number}" if number%3 !=2 else None
                    if number%3 ==0:
                        operation,arguments = "message.sync",{"limit":5}
                    elif number%3 ==1:
                        operation,arguments = "message.send",{"recipients":[actor_fixture[1]["id"]],"kind":"FINDING",
                            "subject":"Synthetic message","body":MEASURED_BODY,"thread":"baseline","paths":["fixture.txt"]}
                    else:
                        operation,arguments = "message.inbox",{"limit":5}
                    atomic_json(client_spec,{"socket":str(workspace.socket_path),"workspace_id":workspace.id,
                        "native":actor["native"],"operation":operation,"arguments":arguments,"key":key})
                    started = time.perf_counter()
                    completed = subprocess.run([sys.executable,str(Path(__file__).resolve()),"--client",str(client_spec)],
                                               env=environment,check=True,capture_output=True,timeout=30)
                    cold_result = json.loads(completed.stdout)
                    response = cold_result["response"]
                    all_observations.append({"phase":"cold","operation":operation,"client_ms":(time.perf_counter()-started)*1000,
                        "response_bytes":len(encode_frame(response)),"rejected_admissions":0,"ok":response.get("ok") is True,"error":response.get("error")})
                    if not response.get("ok"):
                        raise RuntimeError(str(response.get("error")))
                    if key is not None:
                        accepted.append({"workspace_id":workspace.id,"actor_id":actor["id"],"operation":operation,
                                         "arguments":arguments,"key":key,"result":response["data"],"context":cold_result["context"]})
                phases["cold"] = summarize([r for r in all_observations if r["phase"]=="cold"])
                phases["cold"]["client_subprocesses"] = cold_count
                phase("mixed")
                phases["mixed"] = run_clients(connected,lambda i,client:invoke_mix(i,client,"mixed"))
                phases["mixed"].update(summarize([r for r in all_observations if r["phase"]=="mixed"]))
                # Real fixed-duration Git hook occupies slow workers, never fast handlers.
                hook = root/".git/hooks/pre-commit"
                hook.write_text("#!/bin/sh\nsleep 2\n")
                hook.chmod(0o700)
                (root/"hash.bin").write_bytes(b"h"*(1024*1024 if smoke else 64*1024*1024))
                phase("stress")
                slow_ids = []
                job_ids = []
                for index in range(min(4,clients)):
                    (root/f"slow-{index}.txt").write_text(f"Synthetic candidate{index}\n")
                    result = call(connected[index],actor_fixture[index],"commit.execute",
                                  {"paths":[f"slow-{index}.txt"],"message":f"Synthetic load{index}"},f"slow-git-{index}","slow")
                    if "operation_id" not in result:
                        raise RuntimeError("Git load did not admit a durable operation")
                    slow_ids.append(result["operation_id"])
                    result = call(connected[index],actor_fixture[index],"readiness.publish",
                                  {"artifact":f"hash-{index}","paths":["hash.bin"],"evidence":"Synthetic benchmark"},f"slow-hash-{index}","slow")
                    slow_ids.append(result["operation_id"])
                    job = call(connected[index],actor_fixture[index],"job.schedule",
                               {"kind":"reminder","due_us":time.time_ns()//1000,"note":"Synthetic load reminder"},f"slow-job-{index}","slow")
                    job_ids.append(job["id"])
                history_stop = threading.Event()
                history_started = threading.Event()
                history_rows = []
                history_errors = []
                def read_history():
                    try:
                        with Client(workspace.socket_path,workspace_id=workspace.id,operator=True,timeout=30) as history_client:
                            cursor = None
                            while not history_stop.is_set():
                                arguments = {"domain":"messages","limit":100}
                                if cursor:
                                    arguments["cursor"] = cursor
                                result,observation = timed_call(history_client,"operator.history",arguments,None)
                                history_rows.append(observation)
                                if not result.get("ok"):
                                    raise RuntimeError(str(result.get("error")))
                                history_started.set()
                                cursor = result["data"].get("cursor")
                    except Exception as error:  # noqa: BLE001 - Preserve every history-thread failure in the result.
                        history_errors.append(str(error)[:1000])
                history_thread = threading.Thread(target=read_history,name="benchmark-history")
                history_thread.start()
                try:
                    if not history_started.wait(10):
                        raise RuntimeError("History workload did not start: "+str(history_errors))
                    overlap_start = operator.health()
                    if not overlap_start.get("ok"):
                        raise RuntimeError("Cannot observe concurrent slow workload")
                    phases["stress"] = run_clients(connected,lambda i,client:invoke_mix(i,client,"stress"))
                    phases["stress"]["overlap_at_barrier"] = overlap_start["data"]
                finally:
                    history_stop.set()
                    history_thread.join(35)
                    if history_thread.is_alive():
                        raise RuntimeError("History reader did not stop within its deadline")
                phases["stress"].update(summarize([r for r in all_observations if r["phase"]=="stress"]))
                phases["stress"]["history"] = summarize(history_rows)
                phases["stress"]["history_errors"] = history_errors
                deadline = time.monotonic()+60
                while True:
                    with service.store.read() as tx:
                        rows = [dict(tx.connection.execute("SELECT id,state,error_json,result_json FROM operations WHERE id=?",(identifier,)).fetchone()) for identifier in slow_ids]
                        job_rows = [dict(tx.connection.execute("SELECT id,state,last_result_json FROM jobs WHERE id=?",(identifier,)).fetchone()) for identifier in job_ids]
                    if (all(row["state"] not in {"queued","running"} for row in rows)
                            and all(row["state"] not in {"pending","running"} for row in job_rows)):
                        break
                    if time.monotonic()>deadline:
                        raise RuntimeError("Slow operation workload exceeded60seconds")
                    time.sleep(.05)
                phases["stress"]["slow_results"] = rows
                phases["stress"]["job_results"] = job_rows
                if soak_seconds:
                    phase("soak")
                    def sample_soak():
                        stats = json.loads(stats_path.read_text())
                        sizes = {suffix or "main": Path(str(workspace.database_path)+suffix).stat().st_size
                                 if Path(str(workspace.database_path)+suffix).exists() else 0 for suffix in ("", "-wal", "-shm")}
                        return {"elapsed_process_cpu_seconds": stats["usage"]["cpu_seconds"],
                                "peak_rss_bytes": stats["usage"]["peak_rss_bytes"],
                                "health": stats["health"], "database_bytes": sizes}
                    phases["soak"] = soak(connected, actor_fixture, call, sample_soak,
                                          seconds=soak_seconds, rate=soak_rate)
                    phases["soak"].update(summarize([r for r in all_observations if r["phase"]=="soak"]))
                for client in connected:
                    client.close()
                connected.clear()
                phase("overload")
                overload_connections = []
                overload_errors = Counter()
                overload_details = []
                try:
                    for _ in range(320):
                        client = Client(workspace.socket_path,actor_fixture[0]["native"],workspace_id=workspace.id,timeout=1)
                        try:
                            client.connect()
                            overload_connections.append(client)
                        except Exception as error:  # noqa: BLE001 - Record all rejection kinds; unexpected kinds fail acceptance.
                            client.close()
                            overload_errors[getattr(error,"code",type(error).__name__)] += 1
                            if len(overload_details)<20 and getattr(error,"code",None)!="SERVICE_BUSY":
                                overload_details.append({"type":type(error).__name__,"message":str(error)[:1000],
                                                         "errno":getattr(error,"errno",None)})
                    phases["overload"] = {"attempted":320,"connected":len(overload_connections),"rejected":dict(overload_errors),
                                          "unexpected_error_details":overload_details,
                                          "service_resources":json.loads(stats_path.read_text())}
                finally:
                    for client in overload_connections:
                        client.close()
                with service.store.read() as tx:
                    report["receipt_oracle"] = assert_receipts(tx.connection,accepted)
                operator.drain(key="benchmark-drain")
                operator.close()
                operator = None
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=65)
                if process.returncode:
                    raise RuntimeError(f"Service exited {process.returncode}")
        except Exception as error:  # noqa: BLE001 - Persist a failing report and clean up this owned service.
            errors.append({"type":type(error).__name__,"message":str(error)[:2000]})
        finally:
            for client in connected:
                client.close()
            if operator is not None:
                operator.close()
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=65)
                except subprocess.TimeoutExpired:
                    # Only this isolated benchmark's own process group is affected.
                    os.killpg(process.pid,signal.SIGKILL)
                    process.wait()
                    errors.append({"type":"TimeoutError","message":"Owned isolated benchmark process did not stop"})
            if stats_path.exists():
                report["service_resources"] = json.loads(stats_path.read_text())
            profile = [json.loads(line) for line in profile_path.read_text().splitlines()] if profile_path.exists() else[]
            retained_profile = output.with_suffix(".service-profile.jsonl")
            if profile_path.exists():
                retained_profile.write_bytes(profile_path.read_bytes())
                report["service_profile_path"] = str(retained_profile)
            for name,value in phases.items():
                value["service_ms"] = distribution([r["service_ms"] for r in profile if r["phase"]==name])
            driver_after = process_usage()
            report["driver_resources"] = {"before":driver_before,"after":driver_after,
                "cpu_seconds_delta":driver_after["cpu_seconds"]-driver_before["cpu_seconds"]}
            report["phases"] = phases
            report["errors"] = errors
            report["observations"] = all_observations
            oracle = report.get("receipt_oracle",{"ok":False})
            routine_effects = report.get("service_resources",{}).get("effects",{}).get("warm",{})
            report["ok"] = (not errors and oracle["ok"] and all(not phases.get(name,{}).get("errors")
                                and not phases.get(name,{}).get("worker_errors") for name in ("warm","cold","mixed","stress"))
                            and not phases.get("stress",{}).get("history_errors")
                            and all(row["state"]=="succeeded" for row in phases.get("stress",{}).get("slow_results",[]))
                            and all(row["state"]=="succeeded" for row in phases.get("stress",{}).get("job_results",[]))
                            and phases.get("overload",{}).get("rejected",{}).get("SERVICE_BUSY",0)>0
                            and set(phases.get("overload",{}).get("rejected",{}))=={"SERVICE_BUSY"}
                            and not report.get("service_resources",{}).get("handler_errors")
                            and report.get("service_resources",{}).get("profile_observer",{}).get("ok") is True
                            and routine_effects.get("subprocess",0)==0 and routine_effects.get("http",0)==0)
            report["acceptance"] = {"complete_workload":not smoke and clients==100 and operations_per_client>=10
                                      and phases.get("warm",{}).get("operations",0)>=1000
                                      and phases.get("mixed",{}).get("operations",0)>=1000
                                      and phases.get("stress",{}).get("operations",0)>=1000
                                      and report["fixture_counts"]["inactive_actors"]>=10000
                                      and report["fixture_counts"]["historical_messages"]>=100000,
                "warm_service_p95_under_50ms":phases.get("warm",{}).get("service_ms",{}).get("p95",float("inf"))<=50,
                "mixed_client_p95_under_500ms":phases.get("mixed",{}).get("client_ms",{}).get("p95",float("inf"))<=500,
                "stress_client_p95_under_500ms":phases.get("stress",{}).get("client_ms",{}).get("p95",float("inf"))<=500,
                "slow_work_active_at_barrier":phases.get("stress",{}).get("overlap_at_barrier",{}).get("slow_operations",0)>0,
                "no_unexpected_admission_rejections":all(phases.get(name,{}).get("rejected_admissions",0)==0 for name in ("warm","mixed","stress")),
                "stable_package_source":report.get("service_resources",{}).get("source_sha256")==report["source_sha256"]
                    =={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in source_root.glob("*.py")},
                "correctness_and_resource_observations":report["ok"]}
            report["acceptance_passed"] = all(report["acceptance"].values())
            if soak_seconds:
                long_run = phases.get("soak", {})
                report["stage_one_soak"] = {
                    "one_hour_100_clients": clients == 100 and long_run.get("elapsed_seconds", 0) >= 3600,
                    "sustained_20_ops_per_second": soak_rate == 20 and long_run.get("operations", 0) >= soak_seconds * 20 * .9,
                    "service_p95_under_100ms": long_run.get("service_ms", {}).get("p95", float("inf")) < 100,
                    "peak_rss_under_256MiB": report.get("service_resources", {}).get("usage", {}).get("peak_rss_bytes", float("inf")) < 256*1024*1024,
                    "no_rejections_or_errors": "errors" in long_run and not long_run["errors"] and long_run.get("rejected_admissions", 1) == 0 and report["ok"],
                }
                report["stage_one_soak_passed"] = all(report["stage_one_soak"].values())
            atomic_json(output,report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--smoke",action="store_true")
    parser.add_argument("--soak-seconds", type=int, default=0)
    parser.add_argument("--soak-rate", type=int, default=20)
    parser.add_argument("--server",type=Path,help=argparse.SUPPRESS)
    parser.add_argument("--client",type=Path,help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.server:
        return server_process(args.server)
    if args.client:
        return cold_client(args.client)
    if args.output is None:
        parser.error("--output is required")
    options = {"clients":4,"operations_per_client":10,"warm_operations":12,"inactive_actors":12,"history_messages":24} if args.smoke else{}
    report = workload(output=args.output,smoke=args.smoke,soak_seconds=args.soak_seconds,soak_rate=args.soak_rate,**options)
    print(json.dumps({"ok":report["ok"],"output":str(args.output),"phases":report["phases"],"errors":report["errors"]},indent=2))
    passed = report["ok"] and (args.smoke or report["acceptance_passed"])
    if args.soak_seconds and not args.smoke:
        passed = passed and report["stage_one_soak_passed"]
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
