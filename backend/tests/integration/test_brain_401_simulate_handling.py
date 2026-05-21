"""回测 step (simulate) 遇 BRAIN 401 的处理 —— 端到端模拟测试 (2026-05-21).

复现并锁住"task 3329/3332 回测 step 0 成功 / Incorrect authentication credentials"
那一类故障的正确处理,**完全不碰真 BRAIN、不碰真 Redis**:

  - FakeSingleActiveBrain (httpx.MockTransport): 像真 BRAIN 一样 single-active —
    每次 POST /authentication 生成新 token 并使旧 token 失效;simulate / 校验
    都按"cookie 里的 token 是否 == 当前活跃 token"返回 201 / 401。
  - FakeRedis (sync + async 两个 wrapper 共享一个 dict store): 同时给
    CircuitBreaker(同步 redis_pool)和 BrainAdapter session(异步 _get_redis)用,
    模拟跨进程共享的 Redis。

这样真实的 simulate_alpha → _request → _coalesced_reauth → _distributed_reauth
→ authenticate → circuit trip/clear 全链路在测试里跑,验证:
  1. 单次 401 → reload 共享 session 复用,不触发本进程自己登录(不踢别人);
  2. N 并发 401 → fleet 锁让全局只发生 1 次 authenticate;
  3. 持续被踢(外部源)→ circuit 正确 trip;
  4. circuit open → simulate fast-fail,不再打 BRAIN。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from unittest.mock import AsyncMock, patch


# ===========================================================================
# Fake single-active BRAIN (httpx MockTransport)
# ===========================================================================

class FakeSingleActiveBrain:
    """模拟 BRAIN 的 single-active-session 行为:新登录踢旧 token。"""

    def __init__(self):
        self.active_token: str | None = None
        self.logins = 0
        self.sim_ok = 0
        self.sim_401 = 0
        # 测试钩子:每次收到 simulate 前调用(用于注入"外部源踢 session")
        self.before_simulate = None

    def _cookie_token(self, request: httpx.Request) -> str | None:
        ck = request.headers.get("cookie", "")
        for part in ck.split(";"):
            part = part.strip()
            if part.startswith("t="):
                return part[2:]
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/authentication"):
            if request.method == "POST":
                self.logins += 1
                self.active_token = f"tok{self.logins}"  # 新登录踢掉旧 token
                return httpx.Response(
                    201,
                    json={"user": {"id": "BPTEST"}, "token": {"expiry": 14400.0}, "permissions": []},
                    headers={"set-cookie": f"t={self.active_token}; Path=/"},
                )
            # GET 校验 session
            tok = self._cookie_token(request)
            if tok and tok == self.active_token:
                return httpx.Response(200, json={"token": {"expiry": 14400.0}})
            return httpx.Response(401, json={"detail": "Incorrect authentication credentials."})
        if path.endswith("/simulations"):
            if self.before_simulate:
                self.before_simulate()
            tok = self._cookie_token(request)
            if tok and tok == self.active_token:
                self.sim_ok += 1
                return httpx.Response(201, headers={"Location": "https://api.worldquantbrain.com/simulations/FAKE"})
            self.sim_401 += 1
            return httpx.Response(401, json={"detail": "Incorrect authentication credentials."})
        return httpx.Response(404)


# ===========================================================================
# Fake Redis (shared dict store; sync wrapper for circuit, async for adapter)
# ===========================================================================

class FakeRedisStore:
    def __init__(self):
        self.data: dict = {}  # key -> (value, expire_ts | None)

    def get(self, k):
        v = self.data.get(k)
        if v is None:
            return None
        val, exp = v
        if exp is not None and time.time() >= exp:
            self.data.pop(k, None)
            return None
        return val

    def set(self, k, val, ex=None, nx=False):
        if nx and self.get(k) is not None:
            return False
        self.data[k] = (val, time.time() + ex if ex else None)
        return True

    def delete(self, k):
        self.data.pop(k, None)


class FakeSyncRedis:
    """给 CircuitBreaker 用(redis_pool.get_redis_client)。"""
    def __init__(self, store: FakeRedisStore):
        self.s = store

    def get(self, k):
        return self.s.get(k)

    def set(self, k, v, ex=None, nx=False):
        return self.s.set(k, v, ex=ex, nx=nx)

    def delete(self, k):
        self.s.delete(k)


class FakeAsyncRedis:
    """给 BrainAdapter._get_redis 用(异步 redis.from_url 替身)。"""
    def __init__(self, store: FakeRedisStore):
        self.s = store

    async def get(self, k):
        return self.s.get(k)

    async def set(self, k, v, ex=None, nx=False):
        return self.s.set(k, v, ex=ex, nx=nx)

    async def delete(self, k):
        self.s.delete(k)

    async def eval(self, lua, numkeys, key, token):
        # 模拟 _REAUTH_RELEASE_LUA: 仅当值==token 时删除(token-checked release)
        if self.s.get(key) == token:
            self.s.delete(key)
            return 1
        return 0

    async def aclose(self):
        pass


# ===========================================================================
# Harness: 注入到真实 BrainAdapter
# ===========================================================================

def _make_adapter(brain: FakeSingleActiveBrain):
    """造一个真实 BrainAdapter,但 client 走 fake BRAIN(独立 cookie jar = 独立'进程')。"""
    from backend.adapters.brain_adapter import BrainAdapter
    a = BrainAdapter(email="e@test.com", password="pw")
    a.client = httpx.AsyncClient(
        transport=httpx.MockTransport(brain.handle),
        base_url="https://api.worldquantbrain.com",
    )
    return a


@pytest.fixture
def env():
    """共享 store + fake BRAIN + 全部注入。yield (brain, store)。"""
    from backend.adapters.brain_adapter import BrainAdapter

    store = FakeRedisStore()
    brain = FakeSingleActiveBrain()
    BrainAdapter._auth_lock = None  # asyncio.Lock 绑当前 loop,逐测试重置

    patches = [
        patch.object(BrainAdapter, "_get_redis", AsyncMock(return_value=FakeAsyncRedis(store))),
        patch("backend.tasks.redis_pool.get_redis_client", return_value=FakeSyncRedis(store)),
        # 隔离与本测试无关的关注点(sim slot / poll)
        patch.object(BrainAdapter, "_acquire_sim_slot", AsyncMock(return_value=True)),
        patch.object(BrainAdapter, "_release_sim_slot", AsyncMock(return_value=None)),
        patch.object(BrainAdapter, "_wait_for_simulation",
                     AsyncMock(return_value={"success": True, "alpha_id": "A", "metrics": {}})),
    ]
    for p in patches:
        p.start()
    # 清掉可能残留的 circuit
    FakeSyncRedis(store).delete("circuit:brain_auth")
    try:
        yield brain, store
    finally:
        for p in patches:
            p.stop()


# ===========================================================================
# 场景测试
# ===========================================================================

@pytest.mark.asyncio
async def test_single_401_reloads_shared_session_without_extra_login(env):
    """worker 手里的 cookie 被别人顶替(stale)→ simulate 401 → 应 reload 共享
    session 复用、重试成功,且【本进程不额外登录】(不踢别人)。"""
    brain, store = env
    worker = _make_adapter(brain)
    other = _make_adapter(brain)
    try:
        # other(模拟另一进程)登录,成为当前活跃 token 并写入共享 Redis
        await other.authenticate()           # logins=1, active=tok1, redis session=tok1
        assert brain.active_token == "tok1"
        # worker 手里是过期的 cookie(它以为自己有 tok_old)
        worker.client.cookies.set("t", "stale_old")

        logins_before = brain.logins
        res = await worker.simulate_alpha("rank(close)")

        assert res.get("success") is True, res
        # 关键:worker 没有自己重新登录 —— 它 reload 了 other 写的共享 session
        assert brain.logins == logins_before, "worker 不应自己登录(应 reload 复用共享 session)"
    finally:
        await worker.client.aclose()
        await other.client.aclose()


@pytest.mark.asyncio
async def test_concurrent_401_coalesces_to_single_authenticate(env):
    """多个并发 simulate 同时 401(共享 session 失效)→ fleet 锁让全局只发生
    1 次真正的 authenticate,其余 reload 复用。"""
    brain, store = env
    # 共享 session 失效:Redis 里有一个指向已死 token 的 session
    store.set("brain_session:cookies", json.dumps({"t": "dead"}))
    brain.active_token = "dead"
    # 让 dead 立刻失效(模拟被踢):active 置 None,任何 simulate/validate 都 401,
    # 直到有人重新 authenticate
    brain.active_token = None

    adapters = [_make_adapter(brain) for _ in range(5)]
    try:
        results = await asyncio.gather(*[a.simulate_alpha("rank(close)") for a in adapters])
        assert all(r.get("success") for r in results), results
        # 5 个并发只允许 1 次真正登录(fleet 锁 + reload 复用)
        assert brain.logins == 1, f"期望 fleet 锁后仅 1 次 authenticate,实际 {brain.logins}"
    finally:
        for a in adapters:
            await a.client.aclose()


@pytest.mark.asyncio
async def test_persistent_external_eviction_trips_circuit(env):
    """有外部源在每次 simulate 前都重登(持续踢)→ reauth 后仍 401 → circuit 被 trip。"""
    from backend.adapters.brain_adapter import BrainAdapter, BRAIN_AUTH_CIRCUIT
    brain, store = env
    worker = _make_adapter(brain)

    # 外部源:每次 simulate 命中前,用一个独立 client 重新登录,把 worker 的 session 顶掉
    ext = _make_adapter(brain)

    def evict():
        # 同步钩子里不能 await;直接改 fake BRAIN 的活跃 token 模拟"外部刚登录"
        brain.logins += 1
        brain.active_token = f"ext{brain.logins}"

    brain.before_simulate = evict
    try:
        res = await worker.simulate_alpha("rank(close)")
        # worker 怎么 reauth 都会被外部立刻顶掉 → 最终 retryable 失败 + circuit trip
        assert res.get("success") is False
        assert res.get("error_kind") in ("brain_auth_failure", "brain_auth_circuit_open")
        assert BRAIN_AUTH_CIRCUIT.is_open(), "持续 401 应 trip circuit"
    finally:
        brain.before_simulate = None
        await worker.client.aclose()
        await ext.client.aclose()


@pytest.mark.asyncio
async def test_circuit_open_fast_fails_without_hitting_brain(env):
    """circuit 已 open → simulate 直接 fast-fail,不打 BRAIN(sim 计数不增)。"""
    from backend.adapters.brain_adapter import BrainAdapter, BRAIN_AUTH_CIRCUIT
    brain, store = env
    worker = _make_adapter(brain)
    try:
        BRAIN_AUTH_CIRCUIT.trip(reason="test_preopen", ttl_sec=300)
        assert BRAIN_AUTH_CIRCUIT.is_open()
        sims_before = brain.sim_ok + brain.sim_401
        res = await worker.simulate_alpha("rank(close)")
        assert res.get("success") is False
        assert res.get("error_kind") == "brain_auth_circuit_open"
        assert res.get("retryable") is True
        # 关键:fast-fail 没有打到 BRAIN
        assert brain.sim_ok + brain.sim_401 == sims_before, "circuit open 时不应打 BRAIN"
    finally:
        await worker.client.aclose()


@pytest.mark.asyncio
async def test_authenticate_clears_circuit(env):
    """一次成功的 authenticate 应清掉 circuit(恢复信号)。"""
    from backend.adapters.brain_adapter import BrainAdapter, BRAIN_AUTH_CIRCUIT
    brain, store = env
    worker = _make_adapter(brain)
    try:
        BRAIN_AUTH_CIRCUIT.trip(reason="test", ttl_sec=300)
        assert BRAIN_AUTH_CIRCUIT.is_open()
        await worker.authenticate()
        assert not BRAIN_AUTH_CIRCUIT.is_open(), "authenticate 成功应 clear circuit"
    finally:
        await worker.client.aclose()


@pytest.mark.asyncio
async def test_authenticate_keeps_only_this_responses_token(env):
    """[review F1] authenticate 必须用本次响应的 token,而不是从累积 jar 里靠
    迭代顺序猜 token —— 否则在'jar 累积同名 t'(正是触发 CookieConflict 的条件)
    下可能选到 STALE token 存进 Redis / 发给 simulate → 又 401。"""
    brain, store = env
    a = _make_adapter(brain)
    try:
        # 预置一个 stale 't'(不同 path,模拟之前 re-auth 累积、不会被新 set 覆盖)
        a.client.cookies.set("t", "STALE_OLD", domain="api.worldquantbrain.com", path="/old")
        await a.authenticate()  # brain active=tok1; 本次响应 set-cookie t=tok1
        toks = sorted({c.value for c in a.client.cookies.jar if c.name == "t"})
        assert toks == ["tok1"], f"authenticate 后应只持有本次响应的新 token, 实际 {toks}"
        # 且 simulate 用新 token 成功(没被 stale token 污染)
        res = await a.simulate_alpha("rank(close)")
        assert res.get("success") is True, res
    finally:
        await a.client.aclose()
