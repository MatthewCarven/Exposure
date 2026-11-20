"""In-process tests: paths, policy parsing, safe inspection, the budget."""
import collections
import sys

import pytest

from exposure import inspector, paths, policy, registry
from exposure.agent import Budget, Handles


# -- paths ----------------------------------------------------------------------

def test_parse_paths():
    assert paths.parse("config") == (("name", "config"), [])
    assert paths.parse("@12.items[0]") == (("handle", 12), [("attr", "items"), ("item", 0)])
    assert paths.parse("t['a]b'][(1, 2)].x") == (
        ("name", "t"), [("item", "a]b"), ("item", (1, 2)), ("attr", "x")])


@pytest.mark.parametrize("bad", ["", "1abc", "a.", "a[", "a[foo]", "a[len(x)]", "a b"])
def test_parse_rejects(bad):
    with pytest.raises(paths.PathError):
        paths.parse(bad)


# -- policy ---------------------------------------------------------------------

def test_policy_parse():
    p = policy.parse({
        "agent": {"scope": "explicit", "entry": "App.run", "pressure": "<entry>:self.n"},
        "expose": {
            "app.main:config": {"read": True, "write": ["debug", "retries"]},
            "app.workers": {"call": ["restart"]},
            "<entry>:self.patch": True,
        }})
    assert p.scope == "explicit" and p.entry == "App.run"
    r0, r1, r2 = p.rules
    assert (r0.module, r0.root, r0.write) == ("app.main", "config", ["debug", "retries"])
    assert (r1.module, r1.root, r1.segments, r1.call) == (None, "app", [("attr", "workers")],
                                                          ["restart"])
    assert (r2.module, r2.root, r2.segments) == ("<entry>", "self", [("attr", "patch")])


@pytest.mark.parametrize("data", [
    {"agent": {"scope": "everything"}},
    {"expose": {"m:x": {"read": "yes"}}},
    {"expose": {"m:x": {"delete": True}}},
    {"expose": {"m:x.__class__": True}},
    {"expose": {"m:@3": True}},
])
def test_policy_rejects(data):
    with pytest.raises(policy.PolicyError):
        policy.parse(data)


def test_policy_load(tmp_path):
    f = tmp_path / "exposure.toml"
    f.write_text('[expose]\n"__main__:config" = { read = true }\n', encoding="utf-8")
    assert policy.find_default([str(tmp_path)]) == str(f)
    assert policy.load(str(f)).rules[0].root == "config"


# -- registry ------------------------------------------------------------------------

def test_expose_registry():
    def restart(n: int) -> bool:
        return True

    cfg = {"debug": False}
    registry.expose(restart)
    registry.expose(cfg, name="cfg", writable=["debug"])
    registry.expose([1], name="ro", read_only=True)
    snap = registry.snapshot()
    assert snap["cfg"].write == ["debug"] and snap["cfg"].call is False
    assert snap["ro"].write is False and snap["ro"].call is False
    key = next(k for k in snap if k.endswith("restart"))
    assert snap[key].call is True
    for k in list(snap):
        registry.unexpose(k)


# -- inspector: never run the target's code ------------------------------------------

class Spy:
    calls = 0

    @property
    def prop(self):
        Spy.calls += 1
        return 1

    def __repr__(self):
        Spy.calls += 100
        return "Spy()"

    def __getattr__(self, name):
        Spy.calls += 10_000
        raise AttributeError(name)


class SpyList(list):
    def __iter__(self):
        Spy.calls += 1_000_000
        return super().__iter__()


class Slots:
    __slots__ = ("a", "b")


def test_no_user_code_runs():
    Spy.calls = 0
    s = Spy()
    s.x = 5
    assert inspector.preview(s) == f"<{__name__}.Spy>"
    assert isinstance(inspector.get_attr(s, "prop"), inspector.NotEvaluated)
    assert inspector.get_attr(s, "x") == 5
    with pytest.raises(LookupError):
        inspector.get_attr(s, "missing")
    kids = {k[0]: k for k in inspector.children(s)}
    assert kids["x"][3] == 5 and kids["prop"][2] == "property"
    sl = SpyList([1, 2, 3])
    assert inspector.preview(sl) == f"<{__name__}.SpyList>"
    assert [k[3] for k in inspector.children(sl)] == [1, 2, 3]
    assert Spy.calls == 0


def test_dunders_denied():
    with pytest.raises(inspector.AccessDenied):
        inspector.get_attr(object(), "__class__")


def test_slots_and_containers():
    o = Slots()
    o.a = 1
    kids = {k[0]: k for k in inspector.children(o)}
    assert kids["a"][3] == 1 and kids["a"][2] == "slot"
    assert isinstance(kids["b"][3], inspector.NotEvaluated)
    assert inspector.get_attr(o, "a") == 1
    d = {"a": 1, (1, 2): "t", object(): 3}
    segs = [k[1] for k in inspector.children(d)]
    assert ("item", "a") in segs and ("item", (1, 2)) in segs and None in segs
    assert inspector.get_item(d, (1, 2)) == "t"
    assert inspector.get_item([1, 2], -1) == 2
    dq = collections.deque([1, 2])
    assert inspector.preview(dq) == "deque[2] deque([1, 2])"
    assert inspector.preview("x" * 200).endswith("…'")
    assert inspector.preview(1 << 1000) == "<int, 1001 bits>"


def test_frame_children():
    def f():
        local_value = [1]
        return sys._getframe()

    frame = f()
    kids = {k[0]: k for k in inspector.children(frame)}
    assert kids["local_value"][3] == [1]


# -- budget and handles -----------------------------------------------------------------

def test_budget_backs_off_on_pressure():
    b = Budget(duty=0.25, slice_s=0.0, yield_s=0.0001)
    counter = [0]
    b.probe = lambda: counter[0]
    b.begin()
    for _ in range(3):
        counter[0] += 1
        b.check_pressure(force=True)
    assert b.level == 3 and b.duty == pytest.approx(0.25 / 8)
    assert b.yield_s == pytest.approx(0.0008)
    assert b.end() >= 0


def test_handles():
    h = Handles()
    a, b = object(), [1]
    na = h.add(a)
    assert h.add(a) == na and h.add(b) != na
    assert h.get(na)[0] is a
    assert h.release([na]) == 1
    with pytest.raises(LookupError):
        h.get(na)
    assert h.release() == 1 and len(h) == 0
