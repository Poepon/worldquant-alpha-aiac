"""Phase 1b B6 — supervisor reconcile/respawn/park/registry + run_worker entry."""
from backend.pool import run_worker
from backend.pool.supervisor import PoolSupervisor


class _FakeProc:
    _ctr = 0

    def __init__(self):
        _FakeProc._ctr += 1
        self.pid = _FakeProc._ctr
        self._code = None

    def poll(self):
        return self._code

    def die(self, code=1):
        self._code = code

    def terminate(self):
        self._code = -15


class _FakePipe:
    """Minimal MULTI/EXEC shim: buffer delete/sadd, replay atomically on execute."""
    def __init__(self, fr):
        self._fr = fr
        self._ops = []

    def delete(self, k):
        self._ops.append(("delete", k, ()))
        return self

    def sadd(self, k, *vals):
        self._ops.append(("sadd", k, vals))
        return self

    def execute(self):
        for op, k, vals in self._ops:
            if op == "delete":
                self._fr.delete(k)
            else:
                self._fr.sadd(k, *vals)
        self._ops = []


class _FakeRedis:
    def __init__(self):
        self.s = set()

    def delete(self, k):
        self.s = set()

    def sadd(self, k, *vals):
        self.s.update(vals)

    def scard(self, k):
        return len(self.s)

    def pipeline(self, transaction=True):
        return _FakePipe(self)


def _make(*, draining=None):
    fr = _FakeRedis()
    t = {"v": 0.0}
    spawned = []

    def spawn(role):
        spawned.append(role)
        return _FakeProc()

    sup = PoolSupervisor(
        spawn_fn=spawn, now_fn=lambda: t["v"], redis_fn=lambda: fr,
        draining_fn=(draining or (lambda r: False)),
    )
    return sup, fr, t, spawned


def test_reconcile_spawns_to_target_and_registers():
    sup, fr, t, spawned = _make()
    ids = sup.reconcile_once()
    # targets hg=1, s=2, e=1 → 4 workers
    assert len(ids) == 4
    assert spawned.count("s") == 2
    assert spawned.count("hg") == 1
    assert spawned.count("e") == 1
    assert fr.scard("pool:workers:alive") == 4
    # idempotent — second reconcile spawns nothing new (all alive)
    spawned.clear()
    sup.reconcile_once()
    assert spawned == []


def test_dead_worker_respawned_after_backoff():
    sup, fr, t, spawned = _make()
    sup.reconcile_once()                     # spawn all at t=0
    # kill one S worker
    sup._procs["s"][0]["proc"].die()
    spawned.clear()
    t["v"] = 5.0                             # within 10s backoff → pruned, not respawned
    ids = sup.reconcile_once()
    assert spawned == []
    assert len([i for i in ids if i.startswith("s-")]) == 1
    t["v"] = 20.0                            # backoff elapsed → respawn
    ids = sup.reconcile_once()
    assert spawned == ["s"]
    assert len([i for i in ids if i.startswith("s-")]) == 2


def test_draining_role_is_parked():
    drained = {"s"}
    sup, fr, t, spawned = _make(draining=lambda r: r in drained)
    ids = sup.reconcile_once()
    # s is drained → no S workers; hg + e still spawned
    assert spawned.count("s") == 0
    assert spawned.count("hg") == 1 and spawned.count("e") == 1
    assert not any(i.startswith("s-") for i in ids)


def test_terminate_all_clears_registry():
    sup, fr, t, spawned = _make()
    sup.reconcile_once()
    assert fr.scard("k") == 4
    sup.terminate_all()
    assert fr.scard("k") == 0


def test_run_worker_usage_and_flag_off(monkeypatch):
    assert run_worker.main([]) == 2          # no role
    assert run_worker.main(["bogus"]) == 2   # bad role
    # ENABLE_POOL_PIPELINE default OFF → exits 0 without running the loop
    assert run_worker.main(["s"]) == 0
