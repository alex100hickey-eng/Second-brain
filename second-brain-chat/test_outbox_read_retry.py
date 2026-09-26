"""outbox._rows retries a transient read failure before reporting an empty outbox (2026-09-26)."""
import outbox


class _Flaky:
    def __init__(self, fail_times):
        self.calls = 0; self.fail_times = fail_times
    def table(self, *_):
        return self
    def select(self, *_): return self
    def eq(self, *_): return self
    def order(self, *_, **__): return self
    def limit(self, *_): return self
    def execute(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OSError(8, "nodename nor servname provided, or not known")
        class R: data = [{"id": 1, "output_text": '{"kind": "email_draft", "status": "open"}'}]
        return R()


def test_a_transient_failure_is_retried(monkeypatch):
    monkeypatch.setattr(outbox, "READ_RETRY_S", 0)
    flaky = _Flaky(fail_times=2)
    monkeypatch.setattr(outbox, "supabase", flaky)
    rows = outbox._rows(10)
    assert flaky.calls == 3 and len(rows) == 1 and rows[0]["id"] == 1


def test_a_persistent_failure_still_reads_as_empty(monkeypatch, capsys):
    monkeypatch.setattr(outbox, "READ_RETRY_S", 0)
    flaky = _Flaky(fail_times=99)
    monkeypatch.setattr(outbox, "supabase", flaky)
    assert outbox._rows(10) == [] and flaky.calls == outbox.READ_ATTEMPTS
    assert "read failed after" in capsys.readouterr().out
