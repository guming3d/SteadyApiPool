"""scripts/benchmark.py, the load test from the terminal, against the fake gateway."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from tests.litellm.fake_litellm import URL, FakeLiteLLM

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load()


@pytest.fixture
def litellm() -> FakeLiteLLM:
    fake = FakeLiteLLM()
    fake.add_row("gpt6", "azure/gpt6", "https://east.openai.azure.com", id="east")
    return fake


def _key(litellm: FakeLiteLLM) -> str:
    return str(litellm._generate({"key_alias": "cli", "models": ["gpt6"]}).json()["key"])


def test_a_run_prints_the_report_and_writes_the_files(
    litellm: FakeLiteLLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _key(litellm)
    monkeypatch.setenv("LITELLM_KEY", key)
    out = io.StringIO()
    argv = ["--url", f"{URL}/v1", "--model", "gpt6", "--max-requests", "6"]
    argv += ["--json", str(tmp_path / "run.json"), "--csv", str(tmp_path / "run.csv")]

    code = cli.main(argv, transport=litellm.transport(), out=out)

    text = out.getvalue()
    assert code == 0
    assert (
        "Load test of gpt6 at https://gw.example.azurecontainerapps.io/v1/chat/completions" in text
    )
    assert "6 ok, 0 throttled (429)" in text and "east: 6 (100%)" in text
    assert key not in text
    saved = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert saved["summary"]["requests"] == 6 and saved["spec"]["users"] == 2
    assert key not in json.dumps(saved)
    rows = list(csv.DictReader((tmp_path / "run.csv").open(encoding="utf-8")))
    assert len(rows) == 6 and rows[0]["status"] == "200" and rows[0]["member"] == "east"


def test_every_request_failing_exits_1(litellm: FakeLiteLLM) -> None:
    out = io.StringIO()
    argv = ["--url", URL, "--model", "gpt6", "--max-requests", "2", "--key", "sk-wrong"]

    code = cli.main(argv, transport=litellm.transport(), out=out)

    assert code == 1
    assert "errors        401: 2" in out.getvalue()


def test_limits_and_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITELLM_KEY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO())
    with pytest.raises(SystemExit) as too_many:
        cli.main(["--url", URL, "--model", "m", "--users", "500", "--key", "k"])
    with pytest.raises(SystemExit) as no_key:
        cli.main(["--url", URL, "--model", "m"])

    assert too_many.value.code == 2 and no_key.value.code == 2


def test_i_know_lifts_the_limits() -> None:
    args = cli._parser().parse_args(["--url", URL, "--model", "m", "--users", "80", "--i-know"])

    spec = cli._spec(args)

    assert spec.users == 80 and spec.duration_seconds == 30 and spec.prompt_size == "short"


def test_presets_and_options() -> None:
    args = cli._parser().parse_args(
        ["--url", URL, "--model", "m", "--preset", "typical", "--duration", "45", "--no-stream"]
    )

    spec = cli._spec(args)

    assert (spec.users, spec.duration_seconds, spec.ramp_up_seconds) == (10, 45, 10)
    assert spec.stream is False and spec.model == "m"


@pytest.mark.parametrize(
    ("given", "url"),
    [
        ("https://gw", "https://gw/v1/chat/completions"),
        ("https://gw/v1/", "https://gw/v1/chat/completions"),
        ("https://gw/v1/chat/completions", "https://gw/v1/chat/completions"),
    ],
)
def test_the_url(given: str, url: str) -> None:
    assert cli.chat_url(given) == url


def test_apim_sends_the_subscription_key_and_the_api_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3}}
        return httpx.Response(200, json=body, headers={"x-ms-region": "East US 2"})

    monkeypatch.setenv("APIM_KEY", "sub-key")
    monkeypatch.delenv("LITELLM_KEY", raising=False)
    out = io.StringIO()
    argv = ["--gateway", "apim", "--url", "https://apim.azure-api.net/gpt-4o", "--model", "gpt-4o"]
    argv += ["--max-requests", "3", "--no-stream"]

    code = cli.main(argv, transport=httpx.MockTransport(gateway), out=out)

    assert code == 0
    assert "East US 2: 3 (100%)" in out.getvalue()
    request = seen[0]
    assert str(request.url).startswith("https://apim.azure-api.net/gpt-4o/chat/completions?")
    assert request.url.params["api-version"] == cli.APIM_API_VERSION
    assert request.headers["Ocp-Apim-Subscription-Key"] == "sub-key"
    assert "Authorization" not in request.headers
    body = json.loads(request.content)
    assert body["max_completion_tokens"] == 128 and "max_tokens" not in body


def test_apim_options() -> None:
    args = cli._parser().parse_args(
        ["--gateway", "apim", "--url", "https://a/api/", "--model", "m"]
        + ["--key-header", "api-key", "--api-version", "2024-10-21"]
    )

    target = cli.target(args, "k")

    assert target.url == "https://a/api/chat/completions"
    assert target.headers["api-key"] == "k" and target.params == {"api-version": "2024-10-21"}
