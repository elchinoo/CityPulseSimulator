"""Resume after a restart: what was started and not stopped runs again, staggered, once, and a failure never crashes the app or clears the flag."""
import asyncio

import pytest

from cpsim import runtime
from cpsim.model import SpecError
from cpsim.runtime import Runtime
from cpsim.store import MemoryStore

CITY = {"groups": [{"type": "temperature", "count": 2, "protocol": "direct", "frequency": 5}]}
WL = {"kind": "paybroker", "schedule": {"interval": 5, "batch": 1}}
CO = {"employees": {"new": 1, "tenured": 1}, "requests": {"vacation": 2}, "schedule": {"interval": 30}}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for v in ("CPSIM_RESUME_ON_START", "CPSIM_RESUME_STAGGER_SECONDS", "PAYBROKER_URL", "CITYPULSE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("PAYBROKER_SECRET_KEY", "sk_test_x")


def fleet_of(*cities, workloads=(), companies=()):
    rt = Runtime(MemoryStore())
    for n in cities:
        rt.store.save_city(n, {**CITY, "name": n})
    for n in workloads:
        rt.store.save_workload(n, {**WL, "name": n})
    for n in companies:
        rt.store.save_company(n, {**CO, "name": n})
    return rt


def test_starting_remembers_and_the_flags_survive_a_shutdown():
    rt = fleet_of("a", "b", workloads=["w"], companies=["c"])

    async def go():
        await rt.start("a")
        rt.start_workload("w")
        rt.start_company_schedule("c")
        rt.start_hr("c")
        assert rt.wanted("company") == {"c"}
        await asyncio.sleep(0.05)
        await rt.stop_all()                   # the app going down is not a person stopping things

    asyncio.run(go())
    assert rt.wanted("city") == {"a"} and rt.wanted("workload") == {"w"} and rt.wanted("schedule") == {"c"}
    assert rt.wanted("company") == set(), "the one-off run finished by itself (nothing to send to), so it must not start over"


def test_a_failed_start_is_not_remembered():
    rt = fleet_of()
    rt.store.save_city("empty", {"name": "empty", "groups": []})
    with pytest.raises(SpecError):
        asyncio.run(rt.start("empty"))
    assert rt.wanted("city") == set()


def test_resume_starts_everything_flagged_in_order_with_the_stagger(monkeypatch):
    rt = fleet_of("a", "b", "c", workloads=["w"], companies=["co"])
    for kind, name in (("city", "a"), ("city", "c"), ("workload", "w"), ("schedule", "co")):
        rt.want(kind, name, True)
    naps = []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(runtime.asyncio, "sleep", lambda d: naps.append(d) or real_sleep(0 if d == 2.5 else d))

    async def go():
        await rt.resume(stagger=2.5)
        assert set(rt.runners) == {"a", "c"} and rt.wl_runners["w"].running and rt.company_schedules["co"].running   # b was never flagged
        await asyncio.sleep(0.05)
        await rt.stop_all()

    asyncio.run(go())
    assert [n for n in naps if n == 2.5] == [2.5, 2.5, 2.5]            # between four starts, none before the first


def test_an_item_already_started_on_boot_is_not_started_twice():
    rt = fleet_of("a", workloads=["w"])
    rt.want("city", "a", True)
    rt.want("workload", "w", True)

    async def go():
        runner = await rt.start("a")
        wl = rt.start_workload("w")
        await rt.resume(stagger=0)
        assert rt.runners["a"] is runner and rt.wl_runners["w"] is wl
        await asyncio.sleep(0.05)
        await rt.stop_all()

    asyncio.run(go())


def test_a_failing_resume_logs_one_line_keeps_the_flag_and_goes_on(capsys):
    rt = fleet_of("down", "up")
    rt.store.save_city("down", {"name": "down", **CITY, "target_url": "${CITYPULSE_URL}"})          # the fleet address is not set
    rt.want("city", "down", True)
    rt.want("city", "up", True)

    async def go():
        await rt.resume(stagger=0)
        assert set(rt.runners) == {"up"}
        await asyncio.sleep(0.05)
        await rt.stop_all()

    asyncio.run(go())
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "cannot resume" in ln]
    assert len(lines) == 1 and "city down" in lines[0] and "CITYPULSE_URL" in lines[0]
    assert rt.wanted("city") == {"down", "up"}


def test_a_locked_credential_is_a_logged_failure_not_a_crash(monkeypatch, capsys):
    monkeypatch.delenv("PAYBROKER_SECRET_KEY")
    rt = fleet_of(workloads=["w"])
    rt.store.save_workload("w", {**WL, "name": "w", "target_url": "https://pb.example"})
    rt.want("workload", "w", True)
    asyncio.run(rt.resume(stagger=0))
    out = capsys.readouterr().out
    assert out.count("cannot resume workload w") == 1 and "PAYBROKER_SECRET_KEY" in out
    assert rt.wanted("workload") == {"w"} and not rt.wl_runners


def test_a_stopped_or_deleted_item_is_skipped_even_if_its_flag_is_set():
    rt = fleet_of("a")
    rt.want("city", "ghost", True)             # the city was deleted behind its back
    asyncio.run(rt.resume(stagger=0))
    assert rt.runners == {}


def test_the_switch_wins_and_the_environment_is_only_its_default(monkeypatch, capsys):
    rt = fleet_of("a")
    rt.want("city", "a", True)
    assert rt.resume_enabled() is True
    monkeypatch.setenv("CPSIM_RESUME_ON_START", "false")
    assert rt.resume_enabled() is False
    asyncio.run(rt.resume(stagger=0))
    assert rt.runners == {} and "resume on start is off" in capsys.readouterr().out
    rt.store.save_setting("resume_on_start", "true")          # set on the Settings page: beats the environment default
    assert rt.resume_enabled() is True
    rt.store.save_setting("resume_on_start", "false")
    monkeypatch.delenv("CPSIM_RESUME_ON_START")
    assert rt.resume_enabled() is False


def test_the_stagger_setting_is_read_and_clamped(monkeypatch):
    assert runtime.resume_stagger() == 1.0
    for raw, want in (("0", 0.0), ("0.25", 0.25), ("500", 60.0), ("-1", 1.0), ("soon", 1.0), ("nan", 1.0)):
        monkeypatch.setenv("CPSIM_RESUME_STAGGER_SECONDS", raw)
        assert runtime.resume_stagger() == want, raw


def test_a_run_that_finishes_by_itself_is_not_resumed():
    rt = fleet_of()
    rt.store.save_workload("short", {**WL, "name": "short", "schedule": {"interval": 0.2, "batch": 1, "total": 1}})
    rt.store.save_workload("long", {**WL, "name": "long"})

    async def go():
        rt.start_workload("long")
        rt.start_workload("short")
        await rt.wl_runners["short"].wait()
        await asyncio.sleep(0)
        assert rt.wl_runners["short"].state == "done"
        await asyncio.sleep(0.05)
        await rt.stop_all()

    asyncio.run(go())
    assert rt.wanted("workload") == {"long"}


def test_a_store_that_cannot_record_does_not_break_the_run(capsys):
    class Bare(MemoryStore):
        def set_desired(self, kind, name, on):
            raise RuntimeError("database is asleep")
    rt = Runtime(Bare())
    rt.store.save_city("a", {**CITY, "name": "a"})

    async def go():
        await rt.start("a")
        assert "a" in rt.runners
        await asyncio.sleep(0.05)
        await rt.stop_all()

    asyncio.run(go())
    assert "cannot record the run state of city a" in capsys.readouterr().out
